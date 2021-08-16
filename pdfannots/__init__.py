"""
Extracts annotations from a PDF file in markdown format for use in reviewing.
"""

__version__ = '0.1'

import collections
import io
import logging
import typing

from .types import Page, Outline, Annotation
from .utils import cleanup_text, decode_datetime

from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer.pdfpage import PDFPage
from pdfminer.layout import (
    LAParams, LTContainer, LTAnno, LTChar, LTPage, LTTextBox, LTTextLine, LTItem, LTComponent)
from pdfminer.converter import TextConverter
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfdocument import PDFDocument, PDFNoOutlines
from pdfminer.psparser import PSLiteralTable, PSLiteral
import pdfminer.pdftypes as pdftypes
import pdfminer.settings
import pdfminer.utils

pdfminer.settings.STRICT = False

logger = logging.getLogger(__name__)

ANNOT_SUBTYPES = frozenset(
    {'Text', 'Highlight', 'Squiggly', 'StrikeOut', 'Underline'})


def _mkannotation(
    pa: typing.Any,
    page: Page
) -> typing.Optional[Annotation]:

    subtype = pa.get('Subtype')
    if subtype is not None and subtype.name not in ANNOT_SUBTYPES:
        return None

    contents = pa.get('Contents')
    if contents is not None:
        # decode as string, normalise line endings, replace special characters
        contents = cleanup_text(pdfminer.utils.decode_text(contents))

    coords = pdftypes.resolve1(pa.get('QuadPoints'))
    rect = pdftypes.resolve1(pa.get('Rect'))

    author = pdftypes.resolve1(pa.get('T'))
    if author is not None:
        author = pdfminer.utils.decode_text(author)

    created = None
    dobj = pa.get('CreationDate')
    # some pdf apps set modification date, but not creation date
    dobj = dobj or pa.get('ModDate')
    # poppler-based apps (e.g. Okular) use 'M' for some reason
    dobj = dobj or pa.get('M')
    createds = pdftypes.resolve1(dobj)
    if createds is not None:
        createds = pdfminer.utils.decode_text(createds)
        created = decode_datetime(createds)

    return Annotation(page, subtype.name, coords, rect,
                      contents, author=author, created=created)


def _resolve_dest(doc: PDFDocument, dest: typing.Any) -> typing.Any:
    if isinstance(dest, bytes):
        dest = pdftypes.resolve1(doc.get_dest(dest))
    elif isinstance(dest, PSLiteral):
        dest = pdftypes.resolve1(doc.get_dest(dest.name))
    if isinstance(dest, dict):
        dest = dest['D']
    return dest


def _get_outlines(doc: PDFDocument) -> typing.Iterator[Outline]:
    for (_, title, destname, actionref, _) in doc.get_outlines():
        if destname is None and actionref:
            action = pdftypes.resolve1(actionref)
            if isinstance(action, dict):
                subtype = action.get('S')
                if subtype is PSLiteralTable.intern('GoTo'):
                    destname = action.get('D')
        if destname is None:
            continue
        dest = _resolve_dest(doc, destname)

        # consider targets of the form [page /XYZ left top zoom]
        if dest[1] is PSLiteralTable.intern('XYZ'):
            (pageref, _, targetx, targety) = dest[:4]

            if type(pageref) is int or isinstance(pageref, pdftypes.PDFObjRef):
                yield Outline(title, pageref, (targetx, targety))
            else:
                logger.warning("Unsupported pageref in outline: %s", pageref)


class _RectExtractor(TextConverter):  # type:ignore
    # (pdfminer lacks type annotations)

    page: typing.Optional[Page]
    pageseq: int
    _lasthit: typing.FrozenSet[Annotation]
    _curline: typing.Set[Annotation]

    def __init__(
            self,
            rsrcmgr: PDFResourceManager,
            laparams: typing.Optional[LAParams] = None):

        dummy = io.StringIO()
        TextConverter.__init__(self, rsrcmgr, outfp=dummy, laparams=laparams)
        self.page = None
        self.pageseq = 0

    # Called once at the start of each new page
    def start_page(self, page: Page) -> None:
        self.page = page
        self.pageseq = 0

    # callback from parent PDFConverter
    def receive_layout(self, ltpage: LTPage) -> None:
        self._lasthit = frozenset()
        self._curline = set()
        self.render(ltpage)

    def update_pageseq(self, line: LTTextLine) -> None:
        assert self.page is not None
        self.pageseq += 1

        for a in self.page.annots:
            if a.startpos is not None:
                a.startpos.update_pageseq(line, self.pageseq)

        for o in self.page.outlines:
            assert o.pos is not None
            o.pos.update_pageseq(line, self.pageseq)

    def testboxes(self, item: LTComponent) -> typing.AbstractSet[Annotation]:
        assert self.page is not None
        hits = frozenset(
            {a for a in self.page.annots if a.boxes and any(
                {b.hit_item(item) for b in a.boxes})})
        self._lasthit = hits
        self._curline.update(hits)
        return hits

    # "broadcast" newlines to _all_ annotations that received any text on the
    # current line, in case they see more text on the next line, even if the
    # most recent character was not covered.
    def capture_newline(self) -> None:
        for a in self._curline:
            a.capture('\n')
        self._curline = set()

    # This is called once for every layout item on a page, in layout order.
    # Ref: https://pdfminersix.readthedocs.io/en/latest/topic/converting_pdf_to_text.html
    def render(self, item: LTItem) -> None:
        if isinstance(item, LTTextLine):
            self.update_pageseq(item)

        # If it's a container, recurse on nested items.
        if isinstance(item, LTContainer):
            for child in item:
                self.render(child)

            # After the children of a text box, capture the end of the final
            # line (logic derived from pdfminer.converter.TextConverter).
            if isinstance(item, LTTextBox):
                self.testboxes(item)
                self.capture_newline()

        # Each character is represented by one LTChar, and we must handle
        # individual characters (not higher-level objects like LTTextLine)
        # so that we can capture only those covered by the annotation boxes.
        elif isinstance(item, LTChar):
            for a in self.testboxes(item):
                a.capture(item.get_text())

        # LTAnno objects capture whitespace not explicitly encoded in
        # the text. They don't have an (X,Y) position, so we need some
        # heuristics to match them to the nearby annotations.
        elif isinstance(item, LTAnno):
            text = item.get_text()
            if text == '\n':
                self.capture_newline()
            else:
                for a in self._lasthit:
                    a.capture(text)


def process_file(
    file: typing.BinaryIO,
    emit_progress_to: typing.Optional[typing.TextIO] = None,
    laparams: LAParams = LAParams()
) -> typing.Tuple[typing.List[Annotation], typing.List[Outline]]:

    rsrcmgr = PDFResourceManager()
    device = _RectExtractor(rsrcmgr, laparams=laparams)
    interpreter = PDFPageInterpreter(rsrcmgr, device)
    parser = PDFParser(file)
    doc = PDFDocument(parser)

    def emit_progress(msg: str) -> None:
        if emit_progress_to is not None:
            emit_progress_to.write(msg)
            emit_progress_to.flush()

    emit_progress(file.name)

    outlines_by_pageno = collections.defaultdict(list)
    outlines_by_objid = collections.defaultdict(list)

    # Step 1: retrieve outlines if present. Each outline refers to a page, using
    # *either* a PDF object ID or an integer page number. These references will
    # be resolved below while rendering pages -- for now we insert them into one
    # of two dicts for later.
    try:
        for o in _get_outlines(doc):
            if type(o.pageref) is pdftypes.PDFObjRef:
                outlines_by_objid[o.pageref.objid].append(o)
            else:
                outlines_by_pageno[o.pageref].append(o)
    except PDFNoOutlines:
        logger.info("Document doesn't include outlines (\"bookmarks\")")
    except Exception as ex:
        logger.warning("Failed to retrieve outlines: %s", ex)

    # Step 2: iterate over all the pages, using pdfminer to render the text on
    # each page, constructing lists of annotations and (resolved) outlines.
    allannots = []
    alloutlines = []
    for (pageno, pdfpage) in enumerate(PDFPage.create_pages(doc)):
        emit_progress(" %d" % (pageno + 1))

        page = Page(pageno, pdfpage.pageid)

        # Resolve any outlines referring to this page, and link them to the page.
        # Note that outlines may refer to the page number or ID.
        for o in (outlines_by_objid.pop(page.objid, [])
                  + outlines_by_pageno.pop(pageno, [])):
            o.resolve(page)
            page.outlines.append(o)

        # Construct Annotation objects, and append them to the page.
        for pa in pdftypes.resolve1(pdfpage.annots) if pdfpage.annots else []:
            if isinstance(pa, pdftypes.PDFObjRef):
                annot = _mkannotation(pa.resolve(), page)
                if annot is not None:
                    page.annots.append(annot)
            else:
                logger.warning("Unknown annotation: %s", pa)

        # Render the page. This captures the selected text for any annotations
        # on the page, and updates annotations and outlines with a logical
        # sequence number based on the order of text lines on the page.
        device.start_page(page)
        interpreter.process_page(pdfpage)

        # Now we have their logical order, sort the annotations and outlines.
        page.annots.sort()
        page.outlines.sort()
        allannots.extend(page.annots)
        alloutlines.extend(page.outlines)

    emit_progress("\n")

    device.close()

    # all outlines should be resolved by now
    assert {} == outlines_by_pageno
    assert {} == outlines_by_objid

    return (allannots, alloutlines)
