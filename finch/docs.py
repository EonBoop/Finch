"""Text extraction for office files, so read_file gives the model real content instead of zip bytes.
xlsx / docx / pptx are zipped XML (stdlib only); pdf needs pypdf or the pdftotext binary."""
import datetime
import os
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET

DOC_EXTS = (".xlsx", ".xlsm", ".docx", ".pptx", ".pdf")
LEGACY_EXTS = (".xls", ".doc", ".ppt")
MAX_CHARS = 2_000_000

_cache = {}     # path -> (mtime, size, text): ranged reads of one file must not re-parse it


class DocError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text_of(el):
    """Concatenated <t> text under el, with tabs and line breaks kept."""
    out = []
    for n in el.iter():
        k = _local(n.tag)
        if k in ("t", "delText") and n.text:
            out.append(n.text)
        elif k == "tab":
            out.append("\t")
        elif k in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _parse(zf, name):
    try:
        return ET.fromstring(zf.read(name))
    except KeyError:
        return None


# ---------------- xlsx ----------------
_DATE_FMT = re.compile(r"[ymdhs]", re.I)
_BUILTIN_DATE = set(range(14, 23)) | {45, 46, 47}


def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _date_styles(zf):
    """Indices of cell formats (cellXfs) that display dates."""
    root = _parse(zf, "xl/styles.xml")
    if root is None:
        return set()
    custom = {}
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", el.get("formatCode", ""))
            custom[int(el.get("numFmtId"))] = bool(_DATE_FMT.search(code))
    out, idx = set(), 0
    for el in root.iter():
        if _local(el.tag) == "cellXfs":
            for xf in el:
                fid = int(xf.get("numFmtId", 0))
                if fid in _BUILTIN_DATE or custom.get(fid):
                    out.add(idx)
                idx += 1
    return out


def _excel_date(v):
    try:
        d = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=float(v))
    except (ValueError, OverflowError):
        return v
    return d.strftime("%Y-%m-%d") if d.time() == datetime.time(0) else d.strftime("%Y-%m-%d %H:%M")


def _shared_strings(zf):
    root = _parse(zf, "xl/sharedStrings.xml")
    if root is None:
        return []
    return [_text_of(si) for si in root if _local(si.tag) == "si"]


def _sheet_list(zf):
    wb = _parse(zf, "xl/workbook.xml")
    rels = _parse(zf, "xl/_rels/workbook.xml.rels")
    if wb is None:
        raise DocError("not a valid xlsx (no workbook.xml)")
    target = {r.get("Id"): r.get("Target") for r in rels} if rels is not None else {}
    out = []
    for el in wb.iter():
        if _local(el.tag) == "sheet":
            rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
            path = target.get(rid, "")
            path = path.lstrip("/") if path.startswith("/") else "xl/" + path
            out.append((el.get("name", "?"), path))
    return out


def _xlsx(path):
    out = []
    with zipfile.ZipFile(path) as zf:
        if any(n.startswith("xl/encrypted") or n == "EncryptedPackage" for n in zf.namelist()):
            raise DocError("the workbook is encrypted")
        strings, dates = _shared_strings(zf), _date_styles(zf)
        for name, part in _sheet_list(zf):
            if part not in zf.namelist():
                continue
            rows, last_row = [], 0
            with zf.open(part) as f:
                for _, el in ET.iterparse(f):
                    if _local(el.tag) != "row":
                        continue
                    cells, rnum = {}, int(el.get("r", last_row + 1))
                    for c in el:
                        if _local(c.tag) != "c" or not c.get("r"):
                            continue
                        t, v, inline = c.get("t"), None, None
                        for ch in c:
                            k = _local(ch.tag)
                            if k == "v":
                                v = ch.text
                            elif k == "is":
                                inline = _text_of(ch)
                        if inline is not None:
                            val = inline
                        elif v is None:
                            continue
                        elif t == "s":
                            val = strings[int(v)] if int(v) < len(strings) else ""
                        elif t == "b":
                            val = "TRUE" if v == "1" else "FALSE"
                        elif t in ("str", "e", "d"):
                            val = v
                        elif int(c.get("s", 0)) in dates:
                            val = _excel_date(v)
                        else:
                            val = v[:-2] if v.endswith(".0") else v
                        if val != "":
                            cells[_col_index(c.get("r"))] = val.replace("\n", " ").replace("|", "/")
                    el.clear()
                    if cells:
                        row = [cells.get(i, "") for i in range(max(cells) + 1)]
                        rows.append(f"{rnum}: " + " | ".join(row))
                    last_row = rnum
            out.append(f"## Sheet: {name} ({len(rows)} non-empty rows; each line starts with its row number)")
            out.extend(rows or ["(empty)"])
    return "\n".join(out)


# ---------------- docx / pptx ----------------
def _docx(path):
    with zipfile.ZipFile(path) as zf:
        root = _parse(zf, "word/document.xml")
    if root is None:
        raise DocError("not a valid docx (no word/document.xml)")
    body = next((c for c in root if _local(c.tag) == "body"), root)
    out = []

    def para(p):
        style = ""
        for el in p.iter():
            if _local(el.tag) == "pStyle":
                style = next(iter(el.attrib.values()), "")
                break
        text = _text_of(p).strip()
        m = re.match(r"(?:Heading|heading)\s*(\d)", style)
        return ("#" * int(m.group(1)) + " " + text) if (m and text) else text

    for ch in body:
        k = _local(ch.tag)
        if k == "p":
            out.append(para(ch))
        elif k == "tbl":
            for tr in ch.iter():
                if _local(tr.tag) == "tr":
                    cells = [" ".join(_text_of(tc).split()) for tc in tr if _local(tc.tag) == "tc"]
                    out.append("| " + " | ".join(cells) + " |")
            out.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _pptx(path):
    out = []
    with zipfile.ZipFile(path) as zf:
        slides = sorted((n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
        for i, n in enumerate(slides, 1):
            root = _parse(zf, n)
            out.append(f"## Slide {i}")
            out.extend(t for p in root.iter() if _local(p.tag) == "p" and (t := _text_of(p).strip()))
            notes = _parse(zf, n.replace("slides/slide", "notesSlides/notesSlide"))
            if notes is not None:
                nt = " ".join(t for p in notes.iter() if _local(p.tag) == "p" and (t := _text_of(p).strip())
                              and not t.isdigit())
                if nt:
                    out.append("Notes: " + nt)
    return "\n".join(out)


# ---------------- pdf ----------------
def _pdf(path):
    try:
        import pypdf
        r = pypdf.PdfReader(path)
        if r.is_encrypted:
            raise DocError("the PDF is password-protected")
        pages = [f"## Page {i}\n{(p.extract_text() or '').strip()}" for i, p in enumerate(r.pages, 1)]
        return "\n\n".join(pages)
    except ImportError:
        pass
    exe = shutil.which("pdftotext")
    if exe:
        r = subprocess.run([exe, "-layout", path, "-"], capture_output=True, timeout=120)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace").replace("\f", "\n\n---- page break ----\n\n")
        raise DocError("pdftotext failed: " + r.stderr.decode("utf-8", "replace")[:200])
    raise DocError("no PDF reader installed. Install one: `pip install pypdf` or `apt install poppler-utils` "
                   "(pdftotext)")


def extract(path):
    """The file's text. Raises DocError with a message the model can act on."""
    st = os.stat(path)
    hit = _cache.get(path)
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            text = _xlsx(path)
        elif ext == ".docx":
            text = _docx(path)
        elif ext == ".pptx":
            text = _pptx(path)
        elif ext == ".pdf":
            text = _pdf(path)
        else:
            raise DocError(f"unsupported type {ext}")
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as e:
        raise DocError(f"could not parse as {ext[1:]} ({type(e).__name__}: {e})")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[extraction cut at 2M characters]"
    if not text.strip():
        text = "(no extractable text" + (": scanned PDF? it would need OCR)" if ext == ".pdf" else ")")
    _cache[path] = (st.st_mtime, st.st_size, text)
    return text
