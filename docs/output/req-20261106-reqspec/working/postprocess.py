import docx, os
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

P = r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统_需求分析方案_v1.0.docx"
d = docx.Document(P)

names = [s.name for s in d.styles]
print("has Title:", "Title" in names, "| Heading 1:", "Heading 1" in names, "| Heading 4:", "Heading 4" in names)

# ---- 1. cover title -> Title style ----
cover_h1 = None
for p in d.paragraphs:
    if p.style.name == "Heading 1":
        cover_h1 = p
        break
if cover_h1 is not None and "Title" in names:
    cover_h1.style = d.styles["Title"]
    for r in cover_h1.runs:
        r.font.size = docx.shared.Pt(24)
        r.font.bold = True

# ---- 2. shift heading levels: H2->H1, H3->H2 ----
for p in d.paragraphs:
    if p.style.name == "Heading 3":
        p.style = d.styles["Heading 2"]
for p in d.paragraphs:
    if p.style.name == "Heading 2" and p.text.strip() and p is not None:
        # skip already-shifted (previous loop moved H3 to H2; identify by original H2 set)
        pass

# do original H2 -> H1 in a second pass using recorded text list
orig_h2_texts = set()
# recompute: paragraphs currently Heading 2 that were originally Heading 2 are those in TOC list
toc_texts = []
started = False
for p in d.paragraphs:
    if p.text.strip() == "目录":
        started = True
        continue
    if started:
        if p.style.name.startswith("Heading"):
            break
        if p.text.strip():
            toc_texts.append(p.text.strip())

for p in d.paragraphs:
    if p.style.name == "Heading 2" and p.text.strip() in set(toc_texts):
        p.style = d.styles["Heading 1"]

from collections import Counter
print(Counter(x.style.name for x in d.paragraphs).most_common(8))

# ---- 3. footer page numbers on body section ----
def add_field(par, instr, placeholder="1"):
    r = par.add_run()
    fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
    instr_el = OxmlElement("w:instrText"); instr_el.set(qn("xml:space"), "preserve"); instr_el.text = instr
    fld_sep = OxmlElement("w:fldChar"); fld_sep.set(qn("w:fldCharType"), "separate")
    t = OxmlElement("w:t"); t.text = placeholder
    fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
    r._r.append(fld_begin); r._r.append(instr_el); r._r.append(fld_sep); r._r.append(t); r._r.append(fld_end)

body_sec = d.sections[-1]
body_sec.footer.is_linked_to_previous = False
fp = body_sec.footer.paragraphs[0]
for r in list(fp.runs):
    r._r.getparent().remove(r._r)
fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = fp.add_run("第 ")
run.font.size = docx.shared.Pt(8.5)
add_field(fp, " PAGE ")
run = fp.add_run(" 页 / 共 ")
run.font.size = docx.shared.Pt(8.5)
add_field(fp, " NUMPAGES ")
run = fp.add_run(" 页")
run.font.size = docx.shared.Pt(8.5)
for r in fp.runs:
    r.font.size = docx.shared.Pt(8.5)
    r.font.name = "微软雅黑"

# cover footer: empty
cover_sec = d.sections[0]
cover_sec.footer.is_linked_to_previous = False
cfp = cover_sec.footer.paragraphs[0]
for r in list(cfp.runs):
    r._r.getparent().remove(r._r)

# ---- 4. force field update on open ----
settings = d.settings.element
if settings.find(qn("w:updateFields")) is None:
    uf = OxmlElement("w:updateFields")
    uf.set(qn("w:val"), "true")
    settings.append(uf)

d.save(P)
print("saved", round(os.path.getsize(P) / 1024, 1), "KB")
print("toc entries:", len(toc_texts))
