"""PDFium-backed validation and conservative physical-page location."""

import math
from collections.abc import Iterator
from contextlib import closing, contextmanager
from ctypes import byref, c_uint
from io import BytesIO
from threading import Lock

import pypdfium2 as pdfium
from pypdfium2 import PdfiumError, raw

from ira_native.adapters.mineru import NORMALIZED_PAGE, NativeBlock
from ira_native.domain.models import BBox, PdfPageLocation
from ira_native.errors import IraError

_PDFIUM_LOCK = Lock()
_TEXT_BLOCK_KINDS = {
    "text",
    "title",
    "doc_title",
    "paragraph_title",
    "ref_text",
    "caption",
    "header",
    "footer",
    "page_number",
    "footnote",
}


@contextmanager
def open_pdf(data: bytes, max_pages: int) -> Iterator[pdfium.PdfDocument]:
    # PDFium calls must be serialized even across distinct document handles.
    with _PDFIUM_LOCK:
        try:
            with pdfium.PdfDocument(data) as document:
                if len(document) == 0:
                    raise IraError("invalid_pdf")
                if len(document) > max_pages:
                    raise IraError("resource_limit")
                yield document
        except (PdfiumError, KeyError, OverflowError):
            raise IraError("invalid_pdf") from None


@contextmanager
def open_page(document: pdfium.PdfDocument, physical_page: int) -> Iterator[pdfium.PdfPage]:
    if type(physical_page) is not int or not 1 <= physical_page <= len(document):
        raise IraError("invalid_location")
    with closing(document[physical_page - 1]) as page:
        yield page


def page_info(
    asset_id: str, page: pdfium.PdfPage, physical_page: int, page_count: int
) -> PdfPageLocation:
    box = page.get_bbox()
    width, height = page.get_size()
    if not all(math.isfinite(x) for x in (*box, width, height)) or min(width, height) <= 0:
        raise IraError("invalid_pdf")
    return PdfPageLocation(
        asset_id=asset_id,
        page_index=physical_page - 1,
        physical_page=physical_page,
        page_count=page_count,
        page_box=box,
        width=width,
        height=height,
        rotation=page.get_rotation(),
    )


def _contains(outer: BBox, inner: BBox) -> bool:
    return (
        outer[0] <= inner[0] <= inner[2] <= outer[2]
        and outer[1] <= inner[1] <= inner[3] <= outer[3]
    )


def _has_visible_fill(obj: pdfium.PdfTextObj) -> bool:
    if raw.FPDFTextObj_GetTextRenderMode(obj) != raw.FPDF_TEXTRENDERMODE_FILL:
        return False
    red, green, blue, alpha = c_uint(), c_uint(), c_uint(), c_uint()
    ok = raw.FPDFPageObj_GetFillColor(obj, byref(red), byref(green), byref(blue), byref(alpha))
    return bool(ok) and alpha.value > 0


def _matches(text_page: pdfium.PdfTextPage, text: str, region: BBox) -> bool:
    # Bound the work per block and require the exact text to be unique on this page.
    if not text or len(text) > 512 or "\x00" in text:
        return False
    with closing(text_page.search(text, match_case=True, consecutive=True)) as searcher:
        match = searcher.get_next()
        if match is None or searcher.get_next() is not None:
            return False
    start, count = match
    if count > 512 or text_page.get_text_bounded(*region).strip() != text:
        return False
    for index in range(start, start + count):
        obj = text_page.get_textobj(index)
        if (
            obj is None
            or not _has_visible_fill(obj)
            or not _contains(region, text_page.get_charbox(index))
        ):
            return False
    return True


def block_bbox(page: pdfium.PdfPage, block: NativeBlock) -> tuple[BBox | None, str | None]:
    box = block.bbox
    if box is None:
        return None, "bbox_missing"
    if block.coord_system != NORMALIZED_PAGE:
        return None, "unsupported_coord_system"
    if (
        not isinstance(box, list)
        or len(box) != 4
        or not all(type(n) in (int, float) and 0 <= n <= 1 for n in box)
    ):
        return None, "invalid_bbox"
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return None, "invalid_bbox"
    media = page.get_mediabox(fallback_ok=False)
    effective = page.get_bbox()
    crop = page.get_cropbox(fallback_ok=False)
    if (
        page.get_rotation() != 0
        or media is None
        or effective != media
        or (crop is not None and crop != media)
        or effective[:2] != (0, 0)
        or page.get_size() != effective[2:]
    ):
        return None, "page_geometry_unverified"
    if block.kind not in _TEXT_BLOCK_KINDS or not isinstance(block.content, str):
        return None, "block_location_unverified"
    width, height = page.get_size()
    region = (x0 * width, (1 - y1) * height, x1 * width, (1 - y0) * height)
    try:
        with closing(page.get_textpage()) as text_page:
            if _matches(text_page, block.content.strip(), region):
                return region, None
    except PdfiumError:
        pass
    return None, "block_location_unverified"


def render_page(page: pdfium.PdfPage, location: PdfPageLocation) -> bytes:
    scale = min(1.5, 1600 / max(location.width, location.height))
    with closing(page.render(scale=scale)) as bitmap:
        with bitmap.to_pil() as image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()
