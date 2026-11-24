"""Run with python tests/browser/pdfjs_probe.py before|after from the repository root.

Requires the project's virtual environment, installed Microsoft Edge, and
Playwright CLI 0.1.25 already available offline in the npm cache.
"""

import argparse
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

sys.path.insert(0, str(Path.cwd() / "tests"))
from fixtures.synthetic.generate_evidence_pair import generate_pair  # noqa: E402

SEED = """
from pathlib import Path
import sys
from ira_native.application.service import EvidenceService
from ira_native.settings import Settings

with EvidenceService(Settings.from_env()) as service:
    run = service.ingest_pair(Path(sys.argv[1]), Path(sys.argv[2])).parse_run
    block, = service.search_evidence("GPON", parse_run_id=run.parse_run_id).items
    assert block.precision == "block" and block.physical_page == 1
    print(block.evidence_id)
"""

SERVER = """
import sys
from threading import Thread
from uvicorn import Config, Server

server = Server(Config("ira_native.interfaces.api:create_app", factory=True,
                       host="127.0.0.1", port=int(sys.argv[1]),
                       access_log=False, proxy_headers=False))

def watch():
    sys.stdin.read(1)
    server.should_exit = True

Thread(target=watch, daemon=True).start()
server.run()
"""


def run_browser(phase: str) -> None:
    output = Path.cwd() / "data/task-005a-r1"
    output.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
    npx = shutil.which("npx.cmd")
    if npx is None:
        raise RuntimeError("npx.cmd is required")
    with tempfile.TemporaryDirectory(prefix="ira-pdfjs-", dir=output) as temp:
        workspace = Path(temp)
        pdf, artifact, _ = generate_pair(workspace / "inputs")
        db = workspace / "test.sqlite"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        base = [
            npx,
            "--offline",
            "--yes",
            "--package",
            "@playwright/cli@0.1.25",
            "playwright-cli",
            f"-s=ira-pdfjs-{port}",
        ]
        env = {**os.environ, "IRA_DB": str(db), "IRA_ALLOWED_ROOTS": str(pdf.parent)}
        if phase == "before":
            # The baseline viewer/assets match HEAD. Run that read-only snapshot in isolation.
            archived = subprocess.check_output(
                ["git", "archive", "--format=zip", "d24931b", "src/ira_native"]
            )
            with ZipFile(io.BytesIO(archived)) as zipped:
                zipped.extractall(workspace / "baseline")
            env["PYTHONPATH"] = str(workspace / "baseline/src")
        identity = subprocess.check_output(
            [sys.executable, "-c", SEED, str(pdf), str(artifact)], env=env, text=True
        ).strip()
        with (output / f"pdfjs-{phase}-server.log").open("w", encoding="utf-8") as server_log:
            server = subprocess.Popen(
                [sys.executable, "-c", SERVER, str(port)],
                env=env,
                stdin=subprocess.PIPE,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    try:
                        with urlopen(url + "/api/v1/doctor", timeout=1) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Local test server exited") from None
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Local test server was not ready")
                probe_path = workspace / "test.js"
                probe_path.write_text(
                    template.replace("__URL__", url)
                    .replace("__EVIDENCE__", identity)
                    .replace("__PHASE__", phase),
                    encoding="utf-8",
                )
                with (output / f"pdfjs-{phase}.log").open("w", encoding="utf-8") as log:
                    for command in (
                        ["open", url, "--browser=msedge"],
                        ["run-code", "--filename", str(probe_path)],
                    ):
                        subprocess.run(
                            base + command,
                            cwd=workspace,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=True,
                        )
                result = (output / f"pdfjs-{phase}.log").read_text(encoding="utf-8")
                if "### Error" in result or '"verified":true' not in result:
                    raise RuntimeError(f"See PDF.js {phase} log")
                first = result.index("### Result")
                print(result[first : result.index("### Ran", first)].strip())
            finally:
                subprocess.run(base + ["close"], cwd=workspace, capture_output=True)
                server.communicate(b"x", timeout=15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before", "after"))
    run_browser(parser.parse_args().phase)
