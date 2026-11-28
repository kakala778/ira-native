"""Generate the hand-authored TASK-005 browser and replay fixtures."""

import csv
import json
from pathlib import Path

from openpyxl import Workbook
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas


def generate(root: Path) -> tuple[Path, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    pdf = root / "spec.pdf"
    parsed = root / "simulated.json"
    csv_path = root / "devices.csv"
    xlsx_path = root / "devices.xlsx"
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(str(pdf), pagesize=(600, 800), invariant=True)
    pages, shares = [], []
    for idx, (equipment, declared, condition) in enumerate(
        (
            ("OLT", 3, ""),
            ("ONU", 1, ""),
            ("ONU", 2, "indoors"),
        )
    ):
        marker = "GPON VERIFY TRACE" if idx == 0 else f"SYNTHETIC PAGE {idx + 1}"
        record = dict(
            equipment_class=equipment,
            manufacturer_model=f"SYN-{equipment}-{idx + 1}",
            configuration_variant=f"Variant-{idx + 1}",
            property="device_quantity",
            scope=f"Scope-{idx + 1}",
            count_basis="per_device",
            unit="台",
            quantity_role="minimum",
            strength="mandatory",
            quantity="2",
            condition=condition,
            source_revision="REV-S1",
        )
        content = "\n".join(f"{key}: {value}" for key, value in record.items())
        canvas.setFont("Helvetica", 12)
        canvas.drawString(30, 740, marker)
        width = pdfmetrics.stringWidth(marker, "Helvetica", 12)
        canvas.setFont("STSong-Light", 11)
        for line, value in enumerate(content.splitlines()):
            canvas.drawString(30, 680 - 20 * line, value)
        canvas.showPage()
        pages.append(
            {
                "page_idx": idx,
                "blocks": [
                    {
                        "type": "text",
                        "content": marker,
                        "bbox": [28 / 600, 50 / 800, (32 + width) / 600, 64 / 800],
                    },
                    {"type": "text", "content": content},
                ],
            }
        )
        row = {
            key: value for key, value in record.items() if key not in ("quantity_role", "strength")
        }
        shares.append(row | {"quantity": declared, "condition": ""})
    canvas.save()
    parsed.write_text(
        json.dumps(
            {
                "metadata": {"producer": {"version": "4.2.3"}},
                "pages": pages,
                "extensions": {},
                "is_full_document": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=shares[0])
        writer.writeheader()
        writer.writerows(shares)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Synthetic devices"
    sheet.append(list(shares[0]))
    for row in shares:
        sheet.append(list(row.values()))
    workbook.save(xlsx_path)
    return pdf, parsed, csv_path, xlsx_path
