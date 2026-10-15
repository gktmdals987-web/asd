#!/usr/bin/env python3
"""표준 라이브러리만으로 Markdown -> DOCX 변환.
제목(#~######), 본문, 굵게(**), 인라인코드(`), 코드블록(```),
불릿 목록(-, *), 번호 목록, 표(|...|), 인용(>), 수평선(---)을 지원.
"""
import re
import sys
import zipfile
from html import escape

# ---------- OOXML 조각 ----------

def t_run(text, bold=False, italic=False, mono=False):
    rpr = []
    if bold:
        rpr.append('<w:b/>')
    if italic:
        rpr.append('<w:i/>')
    if mono:
        rpr.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>')
        rpr.append('<w:sz w:val="18"/>')
    rpr_xml = f'<w:rPr>{"".join(rpr)}</w:rPr>' if rpr else ''
    # 공백/줄바꿈 보존
    text = escape(text)
    return f'<w:r>{rpr_xml}<w:t xml:space="preserve">{text}</w:t></w:r>'


INLINE_RE = re.compile(r'(\*\*.+?\*\*|`[^`]+`)')


def parse_inline(text):
    """**bold**, `code` 인라인 처리."""
    runs = []
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith('**') and part.endswith('**'):
            runs.append(t_run(part[2:-2], bold=True))
        elif part.startswith('`') and part.endswith('`'):
            runs.append(t_run(part[1:-1], mono=True))
        else:
            runs.append(t_run(part))
    return ''.join(runs)


def para(runs_xml, style=None, extra_ppr=''):
    ppr = ''
    inner = ''
    if style:
        inner += f'<w:pStyle w:val="{style}"/>'
    inner += extra_ppr
    if inner:
        ppr = f'<w:pPr>{inner}</w:pPr>'
    return f'<w:p>{ppr}{runs_xml}</w:p>'


def heading(text, level):
    return para(parse_inline(text), style=f'Heading{level}')


def code_block(lines):
    out = []
    shade = '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
    for i, line in enumerate(lines):
        ppr = (f'<w:pPr><w:pStyle w:val="CodeBlock"/>'
               f'<w:pBdr>{border_sides(i, len(lines))}</w:pBdr>{shade}</w:pPr>')
        run = (f'<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" '
               f'w:cs="Consolas"/><w:sz w:val="18"/></w:rPr>'
               f'<w:t xml:space="preserve">{escape(line) if line else ""}</w:t></w:r>')
        out.append(f'<w:p>{ppr}{run}</w:p>')
    return ''.join(out)


def border_sides(i, n):
    b = ('<w:left w:val="single" w:sz="4" w:space="4" w:color="CCCCCC"/>'
         '<w:right w:val="single" w:sz="4" w:space="4" w:color="CCCCCC"/>')
    if i == 0:
        b = '<w:top w:val="single" w:sz="4" w:space="1" w:color="CCCCCC"/>' + b
    if i == n - 1:
        b = b + '<w:bottom w:val="single" w:sz="4" w:space="1" w:color="CCCCCC"/>'
    return b


def list_item(text, ordered):
    num_id = 2 if ordered else 1
    extra = (f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>')
    return para(parse_inline(text), extra_ppr=extra)


def table(rows):
    # rows: list of list[str]; 첫 행 헤더
    colcount = max(len(r) for r in rows)
    grid = ''.join('<w:gridCol/>' for _ in range(colcount))
    body = []
    borders = ('<w:tblBorders>'
               '<w:top w:val="single" w:sz="4" w:color="999999"/>'
               '<w:left w:val="single" w:sz="4" w:color="999999"/>'
               '<w:bottom w:val="single" w:sz="4" w:color="999999"/>'
               '<w:right w:val="single" w:sz="4" w:color="999999"/>'
               '<w:insideH w:val="single" w:sz="4" w:color="999999"/>'
               '<w:insideV w:val="single" w:sz="4" w:color="999999"/>'
               '</w:tblBorders>')
    tblpr = (f'<w:tblPr><w:tblStyle w:val="TableGrid"/>'
             f'<w:tblW w:w="5000" w:type="pct"/>{borders}</w:tblPr>')
    for ri, row in enumerate(rows):
        cells = []
        is_head = (ri == 0)
        for ci in range(colcount):
            cell_text = row[ci] if ci < len(row) else ''
            shade = ('<w:shd w:val="clear" w:color="auto" w:fill="D9E2F3"/>'
                     if is_head else '')
            tcpr = f'<w:tcPr><w:tcW w:w="0" w:type="auto"/>{shade}</w:tcPr>'
            runs = parse_inline(cell_text)
            if is_head:
                runs = parse_inline(f'**{cell_text}**') if cell_text else runs
            cells.append(f'<w:tc>{tcpr}<w:p>{runs}</w:p></w:tc>')
        body.append(f'<w:tr>{"".join(cells)}</w:tr>')
    return f'<w:tbl>{tblpr}<w:tblGrid>{grid}</w:tblGrid>{"".join(body)}</w:tbl>'


def hr():
    ppr = ('<w:pPr><w:pBdr>'
           '<w:bottom w:val="single" w:sz="6" w:space="1" w:color="999999"/>'
           '</w:pBdr></w:pPr>')
    return f'<w:p>{ppr}</w:p>'


def split_table_row(line):
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|'):
        line = line[:-1]
    return [c.strip() for c in line.split('|')]


def is_sep_row(line):
    return bool(re.match(r'^\s*\|?[\s:|-]+\|?\s*$', line)) and '-' in line

# ---------- 메인 파서 ----------

def convert(md_text):
    lines = md_text.split('\n')
    body = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # 코드블록
        if line.strip().startswith('```'):
            j = i + 1
            buf = []
            while j < n and not lines[j].strip().startswith('```'):
                buf.append(lines[j])
                j += 1
            body.append(code_block(buf))
            i = j + 1
            continue

        # 표
        if '|' in line and line.strip().startswith('|') and i + 1 < n and is_sep_row(lines[i + 1]):
            rows = [split_table_row(line)]
            j = i + 2
            while j < n and lines[j].strip().startswith('|'):
                rows.append(split_table_row(lines[j]))
                j += 1
            body.append(table(rows))
            i = j
            continue

        stripped = line.strip()

        # 수평선
        if stripped in ('---', '***', '___'):
            body.append(hr())
            i += 1
            continue

        # 제목
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            level = len(m.group(1))
            body.append(heading(m.group(2), level))
            i += 1
            continue

        # 인용
        if stripped.startswith('>'):
            qtext = stripped[1:].strip()
            extra = ('<w:ind w:left="360"/>'
                     '<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" '
                     'w:color="BBBBBB"/></w:pBdr>')
            body.append(para(parse_inline(qtext), extra_ppr=extra))
            i += 1
            continue

        # 번호 목록
        m = re.match(r'^(\d+)\.\s+(.*)$', stripped)
        if m:
            body.append(list_item(m.group(2), ordered=True))
            i += 1
            continue

        # 불릿 목록
        m = re.match(r'^[-*]\s+(.*)$', stripped)
        if m:
            body.append(list_item(m.group(1), ordered=False))
            i += 1
            continue

        # 빈 줄
        if stripped == '':
            body.append('<w:p/>')
            i += 1
            continue

        # 일반 문단
        body.append(para(parse_inline(stripped)))
        i += 1

    return ''.join(body)

# ---------- DOCX 패키징 ----------

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''

NUMBERING = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="420" w:hanging="240"/></w:pPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="420" w:hanging="300"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Malgun Gothic" w:eastAsia="Malgun Gothic" w:hAnsi="Malgun Gothic"/><w:sz w:val="20"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="360" w:after="160"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="1F3864"/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="140"/><w:pBdr><w:bottom w:val="single" w:sz="4" w:space="2" w:color="2E5496"/></w:pBdr><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="2E5496"/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="120"/><w:outlineLvl w:val="2"/></w:pPr><w:rPr><w:b/><w:color w:val="2E5496"/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading4"><w:name w:val="heading 4"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="100"/><w:outlineLvl w:val="3"/></w:pPr><w:rPr><w:b/><w:color w:val="44546A"/><w:sz w:val="22"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading5"><w:name w:val="heading 5"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="4"/></w:pPr><w:rPr><w:b/><w:i/><w:color w:val="44546A"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading6"><w:name w:val="heading 6"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/><w:outlineLvl w:val="5"/></w:pPr><w:rPr><w:i/><w:color w:val="44546A"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/><w:ind w:left="120"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="999999"/><w:left w:val="single" w:sz="4" w:color="999999"/><w:bottom w:val="single" w:sz="4" w:color="999999"/><w:right w:val="single" w:sz="4" w:color="999999"/><w:insideH w:val="single" w:sz="4" w:color="999999"/><w:insideV w:val="single" w:sz="4" w:color="999999"/></w:tblBorders></w:tblPr></w:style>
</w:styles>'''

DOC_TMPL = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>{body}'
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
            'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
            '</w:body></w:document>')


def build_docx(md_path, out_path):
    with open(md_path, encoding='utf-8') as f:
        md = f.read()
    body = convert(md)
    document = DOC_TMPL.format(body=body)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CONTENT_TYPES)
        z.writestr('_rels/.rels', RELS)
        z.writestr('word/document.xml', document)
        z.writestr('word/_rels/document.xml.rels', DOC_RELS)
        z.writestr('word/styles.xml', STYLES)
        z.writestr('word/numbering.xml', NUMBERING)
    print(f'OK -> {out_path}')


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else 'Linux_syscall_report.md'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'Linux_syscall_report.docx'
    build_docx(src, dst)
