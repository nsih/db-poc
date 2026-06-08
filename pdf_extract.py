# pdf_extract.py
import re
import logging
from io import BytesIO

import fitz
import pymupdf4llm

logger = logging.getLogger(__name__)


class PdfExtractError(Exception):
    pass


def _merge_page_breaks(md_text: str) -> str:
    page_sep = re.compile(r'\n{0,2}(?<!\|)-{3,}(?!\|)\n{0,2}')
    segments = page_sep.split(md_text)
    merged_parts = []

    for i, seg in enumerate(segments):
        if i == 0:
            merged_parts.append(seg)
            continue
        prev_tail = merged_parts[-1].rstrip()
        cur_head  = seg.lstrip()
        if prev_tail.endswith('|') and cur_head.startswith('|'):
            merged_parts[-1] = prev_tail + '\n' + cur_head
        elif prev_tail and prev_tail[-1] not in '.。!?|#\n':
            merged_parts[-1] = prev_tail + cur_head
        else:
            merged_parts[-1] = prev_tail + '\n\n' + cur_head

    result = ''.join(merged_parts)
    result = re.sub(r'([가-힣a-zA-Z])-\n([가-힣a-zA-Z])', r'\1\2', result)

    lines = result.split('\n')
    out = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\|(?:[-:]+\|)+$', stripped):
            prev = next((l.strip() for l in reversed(out) if l.strip()), '')
            if re.match(r'^\|\s*\d+\s*\|', prev):
                continue
        out.append(line)

    result = '\n'.join(out)
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r'(\|[^\n]+)\n\n+(\|)', r'\1\n\2', result)
    return result


def extract_text_from_pdf(pdf_file_obj: BytesIO) -> str:
    try:
        pdf_file_obj.seek(0)
        doc = fitz.open(stream=pdf_file_obj.read(), filetype="pdf")
        md_text = pymupdf4llm.to_markdown(doc)
        return _merge_page_breaks(md_text)
    except Exception as e:
        logger.error(f"PDF 추출 실패: {e}")
        raise PdfExtractError(f"PDF 파싱 에러: {str(e)}")
