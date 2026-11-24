"""Create a hand-authored PDF and simulated MinerU 4.2-shaped JSON in a new directory."""

import argparse
import json
from pathlib import Path
from typing import Any

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas


def _draw_table(canvas: Canvas, chinese: str, value: str) -> None:
    canvas.rect(20, 100, 160, 20)
    canvas.line(100, 100, 100, 120)
    canvas.setFont("STSong-Light", 10)
    canvas.drawString(25, 105, chinese)
    canvas.setFont("Helvetica", 10)
    if value == "配置 ONU":
        canvas.setFont("STSong-Light", 10)
    canvas.drawString(105, 105, value)


def generate_pair(output: Path) -> tuple[Path, Path, dict[str, Any]]:
    output.mkdir(parents=True, exist_ok=False)
    pdf = output / "synthetic.pdf"
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(str(pdf), pagesize=(200, 200), invariant=True)
    canvas.drawString(20, 150, "OLT GPON GE(0)")
    canvas.drawString(20, 20, "PRINT-IX")
    _draw_table(canvas, "设备清单", "GE(O)")
    canvas.showPage()
    canvas.showPage()  # Physical page 2 is empty.
    canvas.drawString(20, 150, "ONU LAST")
    canvas.drawString(20, 20, "PRINT-XX")
    _draw_table(canvas, "设备", "配置 ONU")
    canvas.showPage()
    canvas.save()
    parsed = output / "simulated-structured-content.json"
    payload = {
        "metadata": {"producer": {"name": "MinerU", "version": "4.2.0"}},
        "extensions": {
            "synthetic_pair": True,
            "fixture_origin": "Hand-authored format simulation; no MinerU execution.",
            "future_field": {"keep": "原样"},
        },
        "is_full_document": True,
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {"type": "text", "content": "OLT GPON GE(0)", "bbox": [0.08, 0.17, 0.65, 0.29]},
                    {
                        "type": "table",
                        "content": "<table><tr><td>设备清单</td><td>GE(O)</td></tr></table>",
                        "bbox": [0.1, 0.4, 0.9, 0.5],
                        "continues_prev": False,
                    },
                ],
            },
            {"page_idx": 1, "blocks": []},
            {
                "page_idx": 2,
                "blocks": [
                    {"type": "text", "content": "ONU LAST", "bbox": [0.08, 0.17, 0.60, 0.29]},
                    {
                        "type": "table",
                        "content": "<table><tr><td>设备</td><td>配置 ONU</td></tr></table>",
                        "bbox": [0.1, 0.4, 0.9, 0.5],
                        "continues_prev": True,
                    },
                ],
            },
        ],
    }
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return pdf, parsed, payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="A new directory for the synthetic pair")
    pdf, artifact, _ = generate_pair(parser.parse_args().directory)
    print(pdf)
    print(artifact)
