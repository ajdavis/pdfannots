import argparse
import logging
import sys
import typing

from pdfminer.layout import LAParams

from . import __doc__, __version__, process_file
from .printer.markdown import MarkdownPrinter, GroupedMarkdownPrinter


def _float_or_disabled(x: str) -> typing.Optional[float]:
    if x.lower().strip() == "disabled":
        return None
    try:
        return float(x)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid float value: {}".format(x))


def parse_args() -> typing.Tuple[argparse.Namespace, LAParams]:
    p = argparse.ArgumentParser(prog='pdfannots', description=__doc__)

    p.add_argument('--version', action='version',
                   version='%(prog)s ' + __version__)

    p.add_argument("input", metavar="INFILE", type=argparse.FileType("rb"),
                   help="PDF files to process", nargs='+')

    g = p.add_argument_group('Basic options')
    g.add_argument("-p", "--progress", default=False, action="store_true",
                   help="emit progress information to stderr")
    g.add_argument("-o", metavar="OUTFILE", type=argparse.FileType("w"), dest="output",
                   default=sys.stdout, help="output file (default is stdout)")

    g = p.add_argument_group('Options controlling output format')
    g.add_argument("-s", "--sections", metavar="SEC", nargs="*",
                   choices=GroupedMarkdownPrinter.ALL_SECTIONS,
                   default=GroupedMarkdownPrinter.ALL_SECTIONS,
                   help=("sections to emit (default: %s)" %
                         ', '.join(GroupedMarkdownPrinter.ALL_SECTIONS)))
    g.add_argument("--no-condense", dest="condense", default=True, action="store_false",
                   help="emit annotations as a blockquote regardless of length")
    g.add_argument("--no-group", dest="group", default=True, action="store_false",
                   help="emit annotations in order, don't group into sections")
    g.add_argument("--print-filename", dest="printfilename", default=False, action="store_true",
                   help="print the filename when it has annotations")
    g.add_argument("-w", "--wrap", metavar="COLS", type=int,
                   help="wrap text at this many output columns")

    g = p.add_argument_group(
        "Layout analysis parameters",
        description="Advanced options affecting PDFMiner text layout analysis.")
    laparams = LAParams()
    g.add_argument(
        "--line-overlap", metavar="REL_HEIGHT", type=float, default=laparams.line_overlap,
        help="If two characters have more overlap than this they are "
             "considered to be on the same line. The overlap is specified "
             "relative to the minimum height of both characters.")
    g.add_argument(
        "--char-margin", metavar="REL_WIDTH", type=float, default=laparams.char_margin,
        help="If two characters are closer together than this margin they "
             "are considered to be part of the same line. The margin is "
             "specified relative to the width of the character.")
    g.add_argument(
        "--word-margin", metavar="REL_WIDTH", type=float, default=laparams.word_margin,
        help="If two characters on the same line are further apart than this "
             "margin then they are considered to be two separate words, and "
             "an intermediate space will be added for readability. The margin "
             "is specified relative to the width of the character.")
    g.add_argument(
        "--line-margin", metavar="REL_HEIGHT", type=float, default=laparams.line_margin,
        help="If two lines are are close together they are considered to "
             "be part of the same paragraph. The margin is specified "
             "relative to the height of a line.")
    g.add_argument(
        "--boxes-flow", type=_float_or_disabled, default=laparams.boxes_flow,
        help="Specifies how much a horizontal and vertical position of a "
             "text matters when determining the order of lines. The value "
             "should be within the range of -1.0 (only horizontal position "
             "matters) to +1.0 (only vertical position matters). You can also "
             "pass 'disabled' to disable advanced layout analysis, and "
             "instead return text based on the position of the bottom left "
             "corner of the text box.")
    g.add_argument(
        "--detect-vertical", default=laparams.detect_vertical,
        action="store_const", const=(not laparams.detect_vertical),
        help="Whether vertical text should be considered during layout analysis")
    g.add_argument(
        "--all-texts", default=laparams.all_texts,
        action="store_const", const=(not laparams.all_texts),
        help="Whether layout analysis should be performed on text in figures.")

    args = p.parse_args()

    # Propagate parsed layout parameters back to LAParams object
    for param in ("line_overlap", "char_margin", "word_margin", "line_margin",
                  "boxes_flow", "detect_vertical", "all_texts"):
        setattr(laparams, param, getattr(args, param))

    return args, laparams


def main() -> None:
    args, laparams = parse_args()
    logging.basicConfig(format='%(levelname)s: %(message)s',
                        level=logging.WARNING)

    # construct Printer instance
    # TODO: replace with appropriate factory logic
    printer = (GroupedMarkdownPrinter if args.group else MarkdownPrinter)(args)

    for file in args.input:
        (annots, outlines) = process_file(
            file,
            emit_progress_to=(sys.stderr if args.progress else None),
            laparams=laparams)
        for line in printer(file.name, annots, outlines):
            args.output.write(line)
