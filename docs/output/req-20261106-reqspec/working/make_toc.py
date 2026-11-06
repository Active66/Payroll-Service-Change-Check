import os, sys, time
import win32com.client as win32

DOC = r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统_需求分析方案_v1.0.docx"
PDF = r"E:\code\PayrollServiceChangeCheck\docs\output\req-20261106-reqspec\working\preview.pdf"

app = win32.DispatchEx("Word.Application")
app.Visible = False
app.DisplayAlerts = 0
try:
    doc = app.Documents.Open(DOC, ReadOnly=False, AddToRecentFiles=False, Visible=False)
    # locate 目录 marker
    target = None
    for i in range(1, min(doc.Paragraphs.Count, 60) + 1):
        p = doc.Paragraphs(i)
        if p.Range.Text.strip() == "目录":
            target = p
            break
    if target is None:
        raise RuntimeError("未找到目录标记段落")
    print("目录段落:", target.Range.Start, target.Range.End)

    # delete the 13 static entries following 目录
    start_del = target.Range.End
    rng = doc.Range(start_del, start_del)
    rng.MoveEnd(4, 13)  # wdParagraph
    rng.Delete()
    print("已删除静态目录条目")

    ins = doc.Range(target.Range.End, target.Range.End)
    toc = doc.TablesOfContents.Add(
        ins,
        True,   # UseHeadingStyles
        1,      # UpperHeadingLevel
        1,      # LowerHeadingLevel
        False,  # IncludePageNumbers handled below
        True,   # RightAlignPageNumbers
        1,      # TabLeader = dots
        True,   # UseHyperlinks
        True,   # HidePageNumbersInWeb
    )
    toc.IncludePageNumbers = True
    toc.Update()
    doc.Fields.Update()
    time.sleep(1)
    print("目录段落数:", toc.Range.Paragraphs.Count)

    doc.Repaginate()
    pages = doc.ComputeStatistics(2)  # wdStatisticPages
    words = doc.ComputeStatistics(0)
    print("pages:", pages, "words:", words)

    doc.Save()
    doc.ExportAsFixedFormat(PDF, 17, False, 0, 0, 0, 0, 0, True, True, 0, True, True, False)
    doc.Close(0)
finally:
    app.Quit()

print("OK", os.path.exists(PDF), round(os.path.getsize(PDF) / 1024, 1), "KB")
