"""Generate a simulated MinerU pair from the checked-in synthetic GPON seed."""

import argparse
import csv
import json
import re
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas


def generate(target: Path) -> tuple[Path, Path, Path]:
    target.mkdir(parents=True, exist_ok=False)
    seed = Path(__file__).parent
    with (seed / "synthetic_requirements.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows.append(rows[1] | {"req_id": "F-04", "scope": "虚构区域C"})
    pdf = target / "synthetic-gpon.pdf"
    parsed = target / "simulated-structured-content.json"
    sheet = target / "synthetic-equipment-list.csv"
    sheet.write_bytes((seed / "synthetic_equipment_list.csv").read_bytes())
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(str(pdf), invariant=True)
    pages = []
    for index, row in enumerate(rows):
        content = "\n".join(
            f"{key}: {value}" for key, value in (row | {"inclusion_status": "in_scope"}).items()
        )
        canvas.setFont("STSong-Light", 14)
        canvas.drawString(45, 780, "IRA Native 合成设备规格")
        for line, value in enumerate(content.splitlines()):
            text = canvas.beginText(45, 745 - 22 * line)
            for span in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]+", value):
                text.setFont("Helvetica" if span.isascii() else "STSong-Light", 14)
                text.textOut(span)
            canvas.drawText(text)
        canvas.showPage()
        pages.append({"page_idx": index, "blocks": [{"type": "text", "content": content}]})
    canvas.save()
    parsed.write_text(
        json.dumps(
            {
                "metadata": {"producer": {"name": "MinerU", "version": "4.2.0"}},
                "extensions": {"synthetic_pair": True, "fixture_origin": "hand-authored format"},
                "is_full_document": True,
                "pages": pages,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pdf, parsed, sheet


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    for path in generate(parser.parse_args().directory):
        print(path)
