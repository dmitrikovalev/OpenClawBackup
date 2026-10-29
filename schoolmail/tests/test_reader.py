"""Offline tests of the /new, /ack and /stats logic against a fake read-only IMAP server.

Run: python -m unittest discover -s tests -v
"""
import dataclasses
import os
import re
import tempfile
import unittest

import reader
import store

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = reader.Config(host="imap.invalid", user="u", password="p",
                    allowed_from=frozenset({"parent@example.com"}),
                    school_domains=frozenset({"greenfield.example"}))
READ_ONLY = {"SELECT", "SEARCH", "FETCH"}


class FakeIMAP:
    """Answers like imaplib.IMAP4_SSL for the read-only subset; any other call fails the test."""

    def __init__(self, messages, uidvalidity=777):
        self.messages = messages        # {uid: raw bytes with CRLF}
        self.uidvalidity = uidvalidity
        self.commands = []

    def select(self, mailbox="INBOX", readonly=False):
        assert readonly, "the mailbox must be opened with EXAMINE"
        self.commands.append("SELECT")
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode() if code == "UIDVALIDITY" else None]

    def uid(self, command, *args):
        command = command.upper()
        self.commands.append(command)
        if command == "SEARCH":
            return "OK", [" ".join(map(str, sorted(self.messages))).encode()]
        if command == "FETCH":
            return "OK", self._fetch(*args)
        raise AssertionError(f"unexpected IMAP command {command}")

    def __getattr__(self, name):        # store, copy, move, expunge, append, close...
        raise AssertionError(f"unexpected IMAP call {name}")

    def _fetch(self, uidset, items):
        assert "BODY.PEEK[" in items and "RFC822 " not in items and "BODY[" not in items, items
        data = [b"3 (FLAGS (\\Seen))"]                  # unsolicited update: must be ignored
        for n, uid in enumerate(map(int, uidset.split(",")), start=1):
            msg = self.messages.get(uid)
            if msg is None:
                continue
            partial = re.search(r"BODY\.PEEK\[\]<0\.(\d+)>", items)
            if "HEADER.FIELDS" in items:
                body, section = msg.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n", "BODY[HEADER.FIELDS (FROM AUTHENTICATION-RESULTS)]"
            elif partial:
                body, section = msg[:int(partial.group(1))], "BODY[]<0>"
            else:
                body, section = msg, "BODY[]"
            date = 'INTERNALDATE "27-Oct-2026 06:00:00 +0000" '
            if uid % 2:     # some servers send UID after the literal
                data += [(f"{n} (RFC822.SIZE {len(msg)} {date}{section} {{{len(body)}}}".encode(), body),
                         f" UID {uid})".encode()]
            else:
                data += [(f"{n} (UID {uid} RFC822.SIZE {len(msg)} {date}{section} {{{len(body)}}}".encode(), body),
                         b")"]
        return data


def sample_mailbox():
    names = sorted(n for n in os.listdir(HERE) if n.endswith(".eml"))
    out = {}
    for i, name in enumerate(names, start=1):
        with open(os.path.join(HERE, name), "rb") as f:
            out[i * 10] = f.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return names, out


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store.Store(os.path.join(self.tmp.name, "state.db"))
        self.names, self.messages = sample_mailbox()
        self.imap = FakeIMAP(self.messages)
        self.uid_of = {name: (i + 1) * 10 for i, name in enumerate(self.names)}

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, *names):
        return [f"777-{self.uid_of[n]}" for n in names]

    def test_new_ack_stats_cycle(self):
        accepted = ["01_school_direct.eml", "02_forward_gmail.eml", "03_forward_outlook.eml",
                    "04_html_newsletter.eml", "05_attachment.eml", "08_broken_encoding.eml",
                    "09_attachments_content.eml"]
        stats = reader.stats(self.imap, self.store, CFG)
        self.assertEqual(stats, {"total": 9, "new": 7, "acked": 0, "skipped_filter": 2})
        self.assertEqual(self.store.processed(777), {})            # /stats writes nothing

        payload, skipped = reader.new_messages(self.imap, self.store, CFG, limit=10)
        self.assertEqual(skipped, 2)
        self.assertEqual([m["id"] for m in payload["messages"]], self.ids(*accepted))
        self.assertEqual(payload["remaining"], 0)
        first = payload["messages"][0]
        self.assertEqual(set(first), {"id", "from", "original_from", "date", "subject", "forwarded",
                                      "original_date", "original_subject", "text", "links",
                                      "attachments", "truncated", "size"})
        broken = payload["messages"][-2]
        self.assertEqual(broken["date"], "2026-10-27T10:00+04:00")  # no Date: INTERNALDATE is used
        self.assertEqual(self.store.processed(777), {
            self.uid_of["06_stranger.eml"]: "skipped", self.uid_of["07_spoofed_forward.eml"]: "skipped"})

        # not acked -> the same messages come again
        again, _ = reader.new_messages(self.imap, self.store, CFG, limit=10)
        self.assertEqual([m["id"] for m in again["messages"]], self.ids(*accepted))

        for value in self.ids(*accepted[:4]):
            uv, uid = reader.parse_id(value)
            self.store.mark(uv, [uid], "acked")
        rest, _ = reader.new_messages(self.imap, self.store, CFG, limit=10)
        self.assertEqual([m["id"] for m in rest["messages"]], self.ids(*accepted[4:]))
        self.assertEqual(reader.stats(self.imap, self.store, CFG),
                         {"total": 9, "new": 3, "acked": 4, "skipped_filter": 2})
        self.assertTrue(set(self.imap.commands) <= READ_ONLY, self.imap.commands)

    def test_limit_and_remaining(self):
        payload, _ = reader.new_messages(self.imap, self.store, CFG, limit=2)
        self.assertEqual([m["id"] for m in payload["messages"]],
                         self.ids("01_school_direct.eml", "02_forward_gmail.eml"))
        self.assertEqual(payload["remaining"], 5)

    def test_new_uidvalidity_starts_over(self):
        reader.new_messages(self.imap, self.store, CFG, limit=50)
        self.imap.uidvalidity = 778
        payload, _ = reader.new_messages(self.imap, self.store, CFG, limit=50)
        self.assertEqual(payload["count"], 7)
        self.assertTrue(all(m["id"].startswith("778-") for m in payload["messages"]))

    def test_check_marks_skipped_and_fetches_headers_only(self):
        result, skipped, ids = reader.check(self.imap, self.store, CFG)
        self.assertEqual((result, skipped), ({"new": 7, "total": 9}, 2))
        self.assertEqual(ids, self.ids("01_school_direct.eml", "02_forward_gmail.eml",
                                       "03_forward_outlook.eml", "04_html_newsletter.eml",
                                       "05_attachment.eml", "08_broken_encoding.eml",
                                       "09_attachments_content.eml"))
        self.assertEqual(sorted(self.store.processed(777).values()), ["skipped", "skipped"])
        self.assertEqual(reader.check(self.imap, self.store, CFG)[:2], ({"new": 7, "total": 9}, 0))
        self.assertEqual(reader.stats(self.imap, self.store, CFG),
                         {"total": 9, "new": 7, "acked": 0, "skipped_filter": 2})
        fetches = [c for c in self.imap.commands if c == "FETCH"]
        self.assertEqual(len(fetches), 3)           # one header fetch per call, no bodies

    def test_unserved(self):
        ids = [f"1-{n}" for n in range(15)]
        self.assertEqual(reader.unserved(ids, {}, now=5000), 10)            # only the next batch
        served = {i: 4000 for i in ids[:10]}                                 # /new gave them out
        self.assertEqual(reader.unserved(ids, served, now=5000), 0)          # failed run: wait
        self.assertEqual(reader.unserved(ids, served, now=4000 + 3600), 10)  # an hour later: retry
        self.assertEqual(reader.unserved(ids[3:], served, now=5000), 3)      # 3 acked: tail is new
        self.assertEqual(reader.unserved([], served, now=5000), 0)

    def test_default_limit_reads_5mb_message_whole(self):
        pdf_b64 = b"JVBERi0xLjQK" + b"QUFB" * (5 * 1024 * 1024 // 4)       # ~5 MB base64 "PDF"
        big = (b"From: Greenfield Office <office@greenfield.example>\r\nSubject: big\r\n"
               b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nNewsletter attached.\r\n--B\r\n"
               b"Content-Type: application/pdf; name=\"news.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
               + pdf_b64 + b"\r\n--B--\r\n")
        payload, _ = reader.new_messages(FakeIMAP({5: big}), self.store, CFG, limit=10)
        m = payload["messages"][0]
        self.assertGreater(m["size"], 5 * 1024 * 1024)
        self.assertFalse(m["truncated"])
        self.assertEqual(m["attachments"][0]["reason"], "broken")    # whole file, just not a real PDF

    def test_large_message_is_read_partially(self):
        big = (b"From: Greenfield Office <office@greenfield.example>\r\nSubject: big\r\n"
               b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
               b"--B\r\nContent-Type: text/plain\r\n\r\nSee the photos.\r\n--B\r\n"
               b"Content-Type: image/jpeg; name=\"p.jpg\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
               + b"QUFB" * 20000 + b"\r\n--B--\r\n")
        imap = FakeIMAP({5: big})
        cfg = dataclasses.replace(CFG, max_bytes=4096)
        payload, _ = reader.new_messages(imap, self.store, cfg, limit=10)
        m = payload["messages"][0]
        self.assertTrue(m["truncated"])
        self.assertEqual(m["size"], len(big))
        self.assertIn("See the photos.", m["text"])

    def test_parse_fetch_variants(self):
        data = [b"1 (FLAGS (\\Seen))",
                (b"2 (UID 7 RFC822.SIZE 12 BODY[] {3}", b"abc"), b")",
                (b"3 (RFC822.SIZE 99 BODY[] {2}", b"xy"), b" UID 9)",
                (b"4 (BODY[] {1}", b"z"), b")"]                 # no UID: dropped
        self.assertEqual([(r["uid"], r.get("size"), r["body"]) for r in reader.parse_fetch(data)],
                         [(7, 12, b"abc"), (9, 99, b"xy")])

    def test_parse_id(self):
        self.assertEqual(reader.parse_id("777-10"), (777, 10))
        for bad in ("777", "a-1", "1-2-3", "", " 1- 2", None, 5):
            self.assertIsNone(reader.parse_id(bad))

    def test_config_from_env(self):
        cfg = reader.Config.from_env({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "secret",
                                      "MAIL_ALLOWED_FROM": " Parent@Example.com , ",
                                      "MAIL_SCHOOL_DOMAINS": "@Greenfield.example,other.example"})
        self.assertEqual(cfg.error, "")
        self.assertEqual(cfg.mailbox, "INBOX")
        self.assertEqual(cfg.port, 993)
        self.assertEqual(cfg.max_bytes, 10 * 1024 * 1024)         # default message size limit
        self.assertEqual(cfg.allowed_from, {"parent@example.com"})
        self.assertEqual(cfg.school_domains, {"greenfield.example", "other.example"})
        self.assertNotIn("secret", repr(cfg))
        self.assertIn("IMAP_HOST", reader.Config.from_env({}).error)


if __name__ == "__main__":
    unittest.main()
