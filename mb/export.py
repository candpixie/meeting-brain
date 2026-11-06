"""Export a meeting as .txt, .md, .docx or .pdf, using whatever is on screen right now:
the names you gave speakers, lines you corrected, and the notes."""
import io
import json
import re
import time
from pathlib import Path

from . import db, langs

# fonts that cover Chinese, Japanese and Korean, first one found wins
PDF_FONTS = ["/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
             "/Library/Fonts/Arial Unicode.ttf",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]


def _ts(t):
    t = int(t or 0)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def gather(mid):
    db.init()  # older databases may lack newer columns
    with db.conn() as c:
        m = c.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        if not m:
            return None
        rows = c.execute("SELECT s.*, k.label FROM segments s JOIN speakers k ON k.id=s.speaker_id "
                         "WHERE s.meeting_id=? ORDER BY start", (mid,)).fetchall()
        people = [r["label"] for r in c.execute(
            "SELECT label FROM speakers WHERE meeting_id=? ORDER BY seconds DESC", (mid,))]
    lines, last = [], None
    for r in rows:  # consecutive lines from the same person read as one turn
        text = db.seg_text(r).strip()
        if not text:
            continue
        if lines and r["label"] == last:
            lines[-1]["text"] += " " + text
        else:
            lines.append({"time": _ts(r["start"]), "who": r["label"], "text": text})
        last = r["label"]
    return {"title": m["title"] or f"Meeting {mid}",
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(m["created"] or 0)),
            "duration": _ts(m["duration"]), "language": langs.get(m["lang"])["name"] if m["lang"] else "",
            "people": people, "notes": json.loads(m["summary"]) if m["summary"] else {}, "lines": lines}


def filename(d, ext):
    slug = re.sub(r"[^\w一-鿿぀-ヿ가-힯-]+", "-", d["title"]).strip("-")[:60] or "meeting"
    return f"{d['date'][:10]} {slug}.{ext}"


def _sections(d, notes=True):
    """(heading, [items]) pairs for the notes part, shared by every format."""
    n = d["notes"] if notes else {}
    out = []
    if n.get("summary"):
        out.append(("Summary", [n["summary"]]))
    if n.get("action_items"):
        out.append(("Action items", [f"{a.get('who', '?')}: {a.get('what', '')}" + (f" (due {a['due']})" if a.get("due") else "")
                                     for a in n["action_items"]]))
    for key, head in (("decisions", "Decisions"), ("open_questions", "Open questions")):
        if n.get(key):
            out.append((head, list(n[key])))
    return out


def _meta(d):
    bits = [d["date"], d["duration"]] + ([d["language"]] if d["language"] else [])
    return " · ".join(bits), ", ".join(d["people"])


def to_txt(d, notes=True):
    meta, people = _meta(d)
    out = [d["title"], meta, f"People: {people}", ""]
    for head, items in _sections(d, notes):
        out += [head.upper(), *[f"- {i}" if head != "Summary" else i for i in items], ""]
    out += ["TRANSCRIPT", ""] + [f"[{l['time']}] {l['who']}: {l['text']}" for l in d["lines"]]
    return "\n".join(out) + "\n"


def to_md(d, notes=True):
    meta, people = _meta(d)
    out = [f"# {d['title']}", "", f"{meta}  ", f"People: {people}", ""]
    for head, items in _sections(d, notes):
        out += [f"## {head}", *[f"- {i}" if head != "Summary" else i for i in items], ""]
    out += ["## Transcript", ""] + [f"**{l['who']}** `{l['time']}`  \n{l['text']}\n" for l in d["lines"]]
    return "\n".join(out) + "\n"


def to_docx(d, notes=True):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Helvetica Neue", Pt(11)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "PingFang TC")  # Chinese/Japanese/Korean glyphs
    doc.add_heading(d["title"], 0)
    meta, people = _meta(d)
    p = doc.add_paragraph(meta + "\n" + f"People: {people}")
    p.runs[0].font.color.rgb = RGBColor(0x77, 0x71, 0x6a)
    for head, items in _sections(d, notes):
        doc.add_heading(head, 1)
        for i in items:
            doc.add_paragraph(i, style=None if head == "Summary" else "List Bullet")
    doc.add_heading("Transcript", 1)
    for l in d["lines"]:
        p = doc.add_paragraph()
        r = p.add_run(l["who"]); r.bold = True
        t = p.add_run("  " + l["time"]); t.font.size = Pt(9); t.font.color.rgb = RGBColor(0x99, 0x93, 0x8a)
        p.add_run("\n" + l["text"])
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_pdf(d, notes=True):
    from fpdf import FPDF
    font = next((f for f in PDF_FONTS if Path(f).exists()), None)
    if not font:
        raise RuntimeError("no font with Chinese/Japanese/Korean characters found for the PDF")
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, 18)
    pdf.add_font("u", "", font)
    pdf.add_page()
    W = pdf.epw

    def para(text, size=10.5, color=(29, 27, 24), gap=1.5):
        pdf.set_font("u", size=size)
        pdf.set_text_color(*color)
        pdf.multi_cell(W, size * 0.5, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(gap)

    meta, people = _meta(d)
    para(d["title"], 20, gap=2)
    para(meta + "\n" + f"People: {people}", 9.5, (119, 113, 106), 5)
    for head, items in _sections(d, notes):
        para(head, 13, (91, 75, 214), 1)
        for i in items:
            para(("• " if head != "Summary" else "") + i)
        pdf.ln(2)
    para("Transcript", 13, (91, 75, 214), 2)
    for l in d["lines"]:
        para(f"{l['who']}   {l['time']}", 9, (119, 113, 106), 0.5)
        para(l["text"], gap=3)
    return bytes(pdf.output())


FORMATS = {"txt": (to_txt, "text/plain; charset=utf-8"), "md": (to_md, "text/markdown; charset=utf-8"),
           "docx": (to_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
           "pdf": (to_pdf, "application/pdf")}
