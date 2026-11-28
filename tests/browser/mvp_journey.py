"""Run TASK-005 through real Edge and isolated loopback servers."""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.request import urlopen

from fixtures.synthetic.mvp_workflow import generate
from pdfjs_probe import SERVER


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_server(stack, env, log, index):
    number = port()
    stream = stack.enter_context(log.open("w", encoding="utf-8"))
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER, str(number)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )

    def stop():
        try:
            proc.communicate(b"x", timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

    stack.callback(stop)
    root = f"http://127.0.0.1:{number}"
    for _ in range(160):
        try:
            with urlopen(root + "/api/v1/doctor", timeout=1) as response:
                if response.status == 200:
                    return root
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    raise RuntimeError(f"Test server {index} did not start")


def run(out: Path, handoffs: Path, ollama: str):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ira-mvp-browser-") as temp, ExitStack() as stack:
        folder = Path(temp)
        pdf, parsed, _, sheet = generate(folder / "synthetic")
        common = {
            **os.environ,
            "IRA_DB": str(folder / "browser.sqlite"),
            "IRA_ENABLE_UPLOADS": "1",
            "IRA_ENABLE_LOCAL_AGENT": "1",
            "IRA_ALLOWED_ROOTS": str(folder),
            "IRA_LOCAL_OLLAMA_MODEL": "qwen3.5:4b",
            "IRA_AGENT_SECONDS": "100",
            "IRA_AGENT_REQUEST_SECONDS": "40",
            "PYDANTIC_AI_NO_BANNER": "1",
        }
        live = start_server(
            stack, common | {"IRA_LOCAL_OLLAMA_URL": ollama}, out / "browser-live-server.txt", 1
        )
        unavailable = start_server(
            stack,
            common
            | {
                "IRA_LOCAL_OLLAMA_URL": f"http://127.0.0.1:{port()}",
                "IRA_AGENT_REQUEST_SECONDS": "0.8",
            },
            out / "browser-model-failure-server.txt",
            2,
        )
        bench = handoffs / "IRA-BENCH-01-R1"
        mineru = (
            handoffs
            / "IRA-BENCH-01-PARSE-RUN/normalized/standard-all/SPEC-IRB-C01/structured_content.json"
        )
        native_pdf = bench / "inputs/pdf/SPEC-IRB-C01.pdf"
        native_sheet = bench / "inputs/lists/LIST-IRB-C01.csv"
        template = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
        variables = {
            "__ROOT__": live,
            "__FAIL__": unavailable,
            "__PDF__": pdf.as_posix(),
            "__PARSED__": parsed.as_posix(),
            "__SHEET__": sheet.as_posix(),
            "__NATIVEPDF__": native_pdf.resolve().as_posix(),
            "__NATIVEJSON__": mineru.resolve().as_posix(),
            "__NATIVESHEET__": native_sheet.resolve().as_posix(),
            "__OUT__": out.resolve().as_posix(),
        }
        for key, value in variables.items():
            template = template.replace(key, value)
        script = folder / "journey.js"
        script.write_text(template, encoding="utf-8")
        base = [
            shutil.which("npx.cmd") or "npx",
            "--offline",
            "--yes",
            "--package",
            "@playwright/cli@0.1.25",
            "playwright-cli",
            "-s=ira-mvp-" + live.rsplit(":", 1)[1],
        ]
        with (out / "edge-journey.txt").open("w", encoding="utf-8") as log:
            try:
                subprocess.run(
                    base + ["open", live, "--browser=msedge"],
                    cwd=folder,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=70,
                )
                subprocess.run(
                    base + ["run-code", "--filename", str(script)],
                    cwd=folder,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=320,
                )
            finally:
                subprocess.run(base + ["close"], cwd=folder, capture_output=True, timeout=20)
        result = (out / "edge-journey.txt").read_text(encoding="utf-8")
        if "### Error" in result or '"verified":true' not in result:
            raise RuntimeError("Edge journey did not verify; see edge-journey.txt")
        print(result[result.index("### Result") :].split("### Ran")[0].strip())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--handoffs", type=Path, default=Path("../../../handoffs"))
    parser.add_argument("--ollama", default="http://127.0.0.1:11445")
    args = parser.parse_args()
    run(args.output.resolve(), args.handoffs.resolve(), args.ollama)
