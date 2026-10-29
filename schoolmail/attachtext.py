"""Text from e-mail attachments: PDF, DOCX, XLSX, PPTX, ICS (structured events), TXT/CSV.

  kind(ctype, name)          -> "pdf" | "docx" | "xlsx" | "pptx" | "ics" | "text" | "html" |
                                "image" | None (unsupported, including macro-enabled Office files)
  read(data, kind, timeout)  -> {"text", "pages"?, "truncated"} | {"events", "truncated"} |
                                {"error": reason}

Reasons: scan (no text layer), encrypted, unsupported, too_large (zip bomb, oversized part,
too complex to finish in time), broken (damaged file). The caller adds "limit" (per-letter
limits), "scan" for pictures and maps unreadable files of a letter cut at the size limit to
too_large. HTML attachments are left to the caller (mailparse.html_to_text).

Attachments come from strangers, so PDF and Office files are parsed in a separate short-lived
process with CPU, memory, file-size and process limits and a wall-clock timeout: a malformed
or hostile file can only fail its own extraction, never the service. Office files are ZIP
archives: the number of members, their total uncompressed size and every member's compression
ratio are checked before anything is unpacked; XML is parsed with defusedxml (no DTDs,
entities or external references) and only text parts are read: macros (vbaProject.bin) and
embedded objects are never extracted or run. Run as a script, this module reads one file from
stdin and prints the JSON result; it imports nothing from the project, so it works under
`python -I`.
"""
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from zoneinfo import ZoneInfo

TEXT_CAP = 3000                     # characters returned per attachment
MAX_PAGES = 30                      # PDF pages / slides / sheets / events looked at
MAX_INPUT = 64 * 1024 * 1024        # bytes the child accepts on stdin
MAX_XML = 20 * 1024 * 1024          # uncompressed size of one Office XML part
MAX_ZIP_FILES = 1000                # members of one Office archive
MAX_ZIP_TOTAL = 100 * 1024 * 1024   # declared uncompressed size of all members
MAX_RATIO = 100                     # compression ratio of a member larger than 1 MB
TIMEOUT = 10                        # seconds per file in the child process
CHILD_KINDS = {"pdf", "docx", "xlsx", "pptx"}
REASONS = {"scan", "encrypted", "unsupported", "too_large", "broken", "limit"}
DUBAI = ZoneInfo("Asia/Dubai")

_EXT = {".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".ics": "ics",
        ".txt": "text", ".csv": "text", ".htm": "html", ".html": "html"}
_TYPES = {"application/pdf": "pdf",
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
          "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
          "text/calendar": "ics", "text/plain": "text", "text/csv": "text", "text/html": "html"}


class Unreadable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def kind(ctype, name):
    """By declared type first, then by file extension (many mailers send application/octet-stream)."""
    ctype = (ctype or "").lower()
    found = _TYPES.get(ctype) or _EXT.get(os.path.splitext(name or "")[1].lower())
    if found:
        return found
    return "image" if ctype.startswith("image/") else None


def _cap(text):
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return text[:TEXT_CAP], len(text) > TEXT_CAP


# ---------------------------------------------------------------- PDF
def _pdf(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")         # opens files protected only by an owner password
        except Exception:                   # e.g. AES without a crypto backend
            ok = False
        if not ok:
            raise Unreadable("encrypted")
    pages, out, total = len(reader.pages), [], 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PAGES or total > TEXT_CAP:
            break
        text = page.extract_text() or ""
        out.append(text)
        total += len(text)
    text = "\n\n".join(out)
    if len(re.sub(r"\s", "", text)) < 20:
        raise Unreadable("scan")
    return {"text": text, "pages": pages, "more": pages > len(out)}


# ---------------------------------------------------------------- Office (ZIP + XML)
def _open_zip(data):
    """Office archive after the zip-bomb checks; nothing is unpacked before they pass."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError):
        raise Unreadable("broken")
    infos = z.infolist()
    if len(infos) > MAX_ZIP_FILES or sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
        raise Unreadable("too_large")
    for i in infos:
        if i.file_size > 1024 * 1024 and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise Unreadable("too_large")
    return z


def _member(z, name):
    """A text part of the archive (never vbaProject.bin or embeddings), size-checked."""
    info = z.getinfo(name)
    if info.file_size > MAX_XML:
        raise Unreadable("too_large")
    return z.open(info)


def _parse(z, name):
    from defusedxml.ElementTree import parse
    with _member(z, name) as f:
        return parse(f, forbid_dtd=True).getroot()


def _numbered(names, pattern):
    found = [(int(m.group(1)), n) for n in names if (m := re.fullmatch(pattern, n))]
    return [n for _, n in sorted(found)]


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w_para(p):
    parts = []
    for el in p.iter():
        if el.tag == W + "t":
            parts.append(el.text or "")
        elif el.tag == W + "tab":
            parts.append(" ")
        elif el.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _w_blocks(parent):
    for child in parent:
        if child.tag == W + "p":
            yield _w_para(child)
        elif child.tag == W + "tbl":
            for tr in child.findall(W + "tr"):
                cells = [" ".join(_w_para(p) for p in tc.iter(W + "p")).strip()
                         for tc in tr.findall(W + "tc")]
                yield " | ".join(cells)
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _w_blocks(content)


def _docx(data):
    with _open_zip(data) as z:
        root = _parse(z, "word/document.xml")
    body = root.find(W + "body")
    return {"text": "\n".join(_w_blocks(body if body is not None else root))}


S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx(data):
    """Rows as "a | b | c"; sheets are streamed and reading stops at TEXT_CAP."""
    from defusedxml.ElementTree import iterparse
    lines, size, more = [], 0, False
    with _open_zip(data) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = ["".join(t.text or "" for t in si.iter(S + "t"))
                      for si in _parse(z, "xl/sharedStrings.xml").iter(S + "si")]
        sheets = _numbered(names, r"xl/worksheets/sheet(\d+)\.xml")
        more = len(sheets) > MAX_PAGES
        for n, name in enumerate(sheets[:MAX_PAGES], start=1):
            if len(sheets) > 1:
                lines.append(f"[Лист {n}]")
            with _member(z, name) as f:
                for _, el in iterparse(f, events=("end",), forbid_dtd=True):
                    if el.tag != S + "row":
                        continue
                    values = []
                    for c in el.iter(S + "c"):
                        v = c.find(S + "v")
                        if c.get("t") == "s" and v is not None and (v.text or "").isdigit():
                            i = int(v.text)
                            values.append(shared[i] if i < len(shared) else "")
                        elif c.get("t") == "inlineStr":
                            values.append("".join(t.text or "" for t in c.iter(S + "t")))
                        else:
                            values.append(v.text or "" if v is not None else "")
                    el.clear()
                    if any(x.strip() for x in values):
                        line = " | ".join(x.strip() for x in values)
                        lines.append(line)
                        size += len(line) + 1
                    if size > TEXT_CAP:
                        return {"text": "\n".join(lines), "more": True}
    return {"text": "\n".join(lines), "more": more}


A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx(data):
    lines = []
    with _open_zip(data) as z:
        slides = _numbered(z.namelist(), r"ppt/slides/slide(\d+)\.xml")
        for n, name in enumerate(slides[:MAX_PAGES], start=1):
            paras = ["".join(t.text or "" for t in p.iter(A + "t")) for p in _parse(z, name).iter(A + "p")]
            paras = [p.strip() for p in paras if p.strip()]
            if paras:
                lines.append(f"[Слайд {n}] " + " / ".join(paras))
    return {"text": "\n".join(lines), "more": len(slides) > MAX_PAGES}


# ---------------------------------------------------------------- calendar, plain text
def _ics_value(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\")).strip()


def _ics_time(params, value):
    """DTSTART/DTEND -> ISO in Asia/Dubai; a date for all-day events; floating time = Dubai."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            return dt.datetime.strptime(value, "%Y%m%d").date().isoformat()
        moment = dt.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        tzid = re.search(r"TZID=([^;:]+)", params)
        if value.endswith("Z"):
            moment = moment.replace(tzinfo=dt.timezone.utc)
        elif tzid:
            moment = moment.replace(tzinfo=ZoneInfo(tzid.group(1).strip('"')))
        else:
            moment = moment.replace(tzinfo=DUBAI)
        return moment.astimezone(DUBAI).isoformat(timespec="minutes")
    except Exception:
        return value[:40] or None


def _ics(data):
    text = re.sub(r"\r?\n[ \t]", "", data.decode("utf-8", "replace"))
    events, cur = [], None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        name = key.split(";")[0].strip().upper()
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {}
        elif name == "END" and value.strip().upper() == "VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and name in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION"):
            cur.setdefault(name, (key, value))
    out = []
    for e in events[:MAX_PAGES]:
        get = lambda k: _ics_value(e[k][1]) if k in e else None
        out.append({"summary": get("SUMMARY"),
                    "start": _ics_time(*e["DTSTART"]) if "DTSTART" in e else None,
                    "end": _ics_time(*e["DTEND"]) if "DTEND" in e else None,
                    "location": get("LOCATION"),
                    "description": (get("DESCRIPTION") or "")[:500] or None})
    return {"events": out, "more": len(events) > MAX_PAGES}


def _text(data):
    try:
        return {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"text": data.decode("cp1251", "replace")}


HANDLERS = {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "ics": _ics, "text": _text}


def extract(data, kind_):
    """In-process extraction (the child process calls this for PDF and Office files)."""
    handler = HANDLERS.get(kind_)
    if handler is None:
        return {"error": "unsupported"}
    try:
        got = handler(data)
    except Unreadable as e:
        return {"error": e.reason}
    except Exception:
        return {"error": "broken"}
    if "events" in got:
        return {"events": got["events"], "truncated": got["more"]}
    text, cut = _cap(got["text"])
    result = {"text": text, "truncated": cut or got.get("more", False)}
    if got.get("pages"):
        result["pages"] = got["pages"]
    return result


# ---------------------------------------------------------------- isolation
def read(data, kind_, timeout=TIMEOUT):
    """Extract; PDF and Office files go through a limited child process."""
    if kind_ not in CHILD_KINDS:
        return extract(data, kind_)
    try:
        proc = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), kind_],
                              input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "too_large"}
    except OSError:
        return {"error": "broken"}
    if proc.returncode != 0:
        # killed by the CPU limit or out of memory: too complex; anything else: damaged
        return {"error": "too_large" if proc.returncode < 0 else "broken"}
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        return {"error": "broken"}
    if not isinstance(result, dict) or not (isinstance(result.get("text"), str)
                                            or result.get("error") in REASONS):
        return {"error": "broken"}
    return result


def _limit_child():
    import resource
    for res, value in ((resource.RLIMIT_CPU, 8), (resource.RLIMIT_AS, 768 * 1024 * 1024),
                       (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NPROC, 0)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _limit_child()
    import logging
    import warnings
    logging.disable(logging.CRITICAL)           # pypdf warnings could quote the file
    warnings.simplefilter("ignore")
    out = extract(sys.stdin.buffer.read(MAX_INPUT), sys.argv[1] if len(sys.argv) > 1 else "")
    sys.stdout.write(json.dumps(out, ensure_ascii=True))
