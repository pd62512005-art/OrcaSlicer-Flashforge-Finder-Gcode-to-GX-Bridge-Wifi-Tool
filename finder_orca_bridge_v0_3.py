#!/usr/bin/env python3
"""OctoPrint-compatible OrcaSlicer bridge for the original FlashForge Finder.

The bridge accepts Orca's ordinary text G-code, creates a valid Finder
xgcode 1.0 (.gx) container, then uploads it through the Finder's legacy
TCP/8899 protocol.

No third-party packages are required.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from finder_gx_v1_0 import GXError, GX_VERSION, build_gx, validate_gx
from finder_lan_v1_3 import FinderClient, FinderProtocolError

VERSION = "0.3.0"
OCTOPRINT_EMULATION_VERSION = "1.11.7"


def decode_response(data: bytes) -> str:
    return data.replace(b"\x00", b"").decode("utf-8", "replace")


def parse_m119(text: str) -> dict:
    out: dict[str, object] = {}
    for key in ("MachineStatus", "MoveMode"):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()
    m = re.search(r"^Status:\s*S:(\d+)\s+L:(\d+)\s+J:(\d+)\s+F:(\d+)", text, re.MULTILINE)
    if m:
        out["flags"] = {"S": int(m.group(1)), "L": int(m.group(2)), "J": int(m.group(3)), "F": int(m.group(4))}
    m = re.search(r"^Endstop:\s*X-max:(\d+)\s+Y-max:(\d+)\s+Z-max:(\d+)", text, re.MULTILINE)
    if m:
        out["endstops"] = {"x": int(m.group(1)), "y": int(m.group(2)), "z": int(m.group(3))}
    return out


def parse_m105(text: str) -> dict:
    m = re.search(r"T0:([-+]?\d+(?:\.\d+)?)\s*/([-+]?\d+(?:\.\d+)?)\s+B:([-+]?\d+(?:\.\d+)?)/([-+]?\d+(?:\.\d+)?)", text)
    if not m:
        return {"tool0": {"actual": 0.0, "target": 0.0}, "bed": {"actual": 0.0, "target": 0.0}}
    return {
        "tool0": {"actual": float(m.group(1)), "target": float(m.group(2))},
        "bed": {"actual": float(m.group(3)), "target": float(m.group(4))},
    }


def parse_m27(text: str) -> tuple[int, int]:
    m = re.search(r"SD printing byte\s+(\d+)/(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


@dataclass
class BridgeState:
    source_filename: str | None = None
    filename: str | None = None
    source_size: int | None = None
    filesize: int | None = None
    converted: bool | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    remote_path: str | None = None
    queued_at: float | None = None
    uploaded_at: float | None = None
    started_at: float | None = None
    phase: str = "IDLE"
    last_error: str | None = None


class FinderBridge:
    def __init__(self, printer_host: str, printer_port: int, timeout: float, spool: Path):
        self.printer_host = printer_host
        self.printer_port = printer_port
        self.timeout = timeout
        self.spool = spool
        self.spool.mkdir(parents=True, exist_ok=True)
        self.state_lock = threading.RLock()
        self.printer_lock = threading.Lock()
        self.state = BridgeState()
        self.worker: threading.Thread | None = None
        self.last_temps = {"tool0": {"actual": 0.0, "target": 0.0}, "bed": {"actual": 0.0, "target": 0.0}}

    def client(self) -> FinderClient:
        # 0.5 s finalization delay is taken from the supplied FlashPrint capture.
        return FinderClient(self.printer_host, self.printer_port, self.timeout, finalize_delay=0.5)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def prepare_upload(self, upload_name: str, upload_bytes: bytes) -> tuple[Path, str, dict[str, object], bool]:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload_name).name)
        if not safe:
            raise ValueError("Upload filename is empty")
        stamp = str(time.time_ns())
        source_path = self.spool / f"{stamp}_source_{safe}"
        source_path.write_bytes(upload_bytes)

        already_gx = upload_bytes.startswith(GX_VERSION)
        gx_bytes = build_gx(upload_bytes)
        info = validate_gx(gx_bytes)
        remote_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(safe).stem) + ".gx"
        gx_path = self.spool / f"{stamp}_{remote_name}"
        gx_path.write_bytes(gx_bytes)

        with self.state_lock:
            self.state = BridgeState(
                source_filename=safe,
                filename=remote_name,
                source_size=len(upload_bytes),
                filesize=len(gx_bytes),
                converted=not already_gx,
                metadata=info,
                queued_at=time.time(),
                phase="PREPARED",
            )
        return gx_path, remote_name, info, not already_gx

    def queue_upload(self, gx_path: Path, remote_name: str, print_now: bool) -> str:
        with self.state_lock:
            if self._busy():
                raise ValueError("A Finder upload is already in progress")
            predicted_remote = f"0:/user/{remote_name}"
            self.state.remote_path = predicted_remote
            self.state.phase = "QUEUED"
            self.state.last_error = None
            self.worker = threading.Thread(
                target=self._upload_worker,
                args=(gx_path, remote_name, print_now),
                name="finder-upload",
                daemon=True,
            )
            self.worker.start()
            return predicted_remote

    def _upload_worker(self, gx_path: Path, remote_name: str, print_now: bool) -> None:
        try:
            with self.state_lock:
                self.state.phase = "UPLOADING"
            print(f"Uploading valid GX to {self.printer_host}: {remote_name} ({gx_path.stat().st_size:,} bytes)", flush=True)
            with self.printer_lock, self.client() as printer:
                remote = printer.upload(gx_path, remote_name=remote_name)
                with self.state_lock:
                    self.state.remote_path = remote
                    self.state.uploaded_at = time.time()
                    self.state.phase = "UPLOADED"
                print(f"Saved as {remote}", flush=True)
                if print_now:
                    with self.state_lock:
                        self.state.phase = "STARTING"
                    response = decode_response(printer.start_print(remote)).strip()
                    print(response, flush=True)
                    with self.state_lock:
                        self.state.started_at = time.time()
                        self.state.phase = "PRINTING"
            print("Finder upload task complete.", flush=True)
        except Exception as exc:
            with self.state_lock:
                self.state.phase = "ERROR"
                self.state.last_error = str(exc)
            print(f"UPLOAD ERROR: {exc}", flush=True)

    def snapshot(self) -> dict:
        with self.state_lock:
            state_copy = asdict(self.state)
            busy = self._busy()

        # Do not compete for the Finder socket while a file transfer is active.
        if busy:
            phase = state_copy["phase"]
            return {
                "machine": phase,
                "printing": False,
                "status": {"MachineStatus": phase},
                "temperatures": self.last_temps,
                "progress": {"current": 0, "total": 0, "completion": None},
                "job": state_copy,
            }

        try:
            with self.printer_lock, self.client() as printer:
                m119 = decode_response(printer.command("M119"))
                m105 = decode_response(printer.command("M105"))
                m27 = decode_response(printer.command("M27"))
            status = parse_m119(m119)
            temps = parse_m105(m105)
            self.last_temps = temps
            current, total = parse_m27(m27)
            machine = str(status.get("MachineStatus", "UNKNOWN"))
            printing = machine == "BUILDING_FROM_SD"
            completion = None
            # Finder reports 0/1000 during warm-up and while idle.
            if printing and total > 1000 and 0 <= current <= total:
                completion = current * 100.0 / total
            with self.state_lock:
                if printing:
                    self.state.phase = "PRINTING"
                elif self.state.phase in ("PRINTING", "STARTING") and machine == "READY":
                    self.state.phase = "READY"
                state_copy = asdict(self.state)
            return {
                "machine": machine,
                "printing": printing,
                "status": status,
                "temperatures": temps,
                "progress": {"current": current, "total": total, "completion": completion},
                "job": state_copy,
            }
        except Exception as exc:
            with self.state_lock:
                self.state.last_error = str(exc)
                state_copy = asdict(self.state)
            return {
                "machine": "OFFLINE",
                "printing": False,
                "status": {"MachineStatus": "OFFLINE"},
                "temperatures": self.last_temps,
                "progress": {"current": 0, "total": 0, "completion": None},
                "job": state_copy,
            }

    def job_command(self, command: str, action: str | None = None) -> None:
        if self._busy():
            raise ValueError("Printer command unavailable while upload is in progress")
        with self.printer_lock, self.client() as printer:
            if command == "cancel":
                printer.command("M26")
                with self.state_lock:
                    self.state.phase = "CANCELLED"
            elif command == "pause":
                # M25/M24 were not present in the supplied capture; retained as experimental.
                if action == "resume":
                    printer.command("M24")
                else:
                    printer.command("M25")
            elif command == "restart":
                raise ValueError("Restart is not implemented for the Finder")
            else:
                raise ValueError(f"Unsupported job command: {command}")


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "FinderOrcaBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> FinderBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

    def handle_expect_100(self) -> bool:
        self.send_response_only(HTTPStatus.CONTINUE)
        self.end_headers()
        return True

    def send_json(self, obj, status=HTTPStatus.OK) -> None:
        raw = b"" if status == HTTPStatus.NO_CONTENT else json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def send_html(self, html: str, status=HTTPStatus.OK) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/version":
                self.send_json({"api": "0.1", "server": OCTOPRINT_EMULATION_VERSION, "text": f"OctoPrint {OCTOPRINT_EMULATION_VERSION}"})
            elif path == "/api/settings":
                self.send_json({"feature": {"sdSupport": True}, "serial": {"autoconnect": True}, "server": {"commands": {}}})
            elif path in ("/api/printerprofiles", "/api/printerprofiles/_default"):
                profile = {
                    "id": "_default", "name": "FlashForge Finder", "default": True, "current": True,
                    "volume": {"formFactor": "rectangular", "origin": "center", "width": 140, "depth": 140, "height": 140, "heatedBed": False},
                    "extruder": {"count": 1, "offsets": [[0.0, 0.0]]},
                }
                self.send_json(profile if path.endswith("_default") else {"profiles": {"_default": profile}})
            elif path == "/api/connection":
                snap = self.bridge.snapshot()
                self.send_json({"current": {"state": snap["machine"], "port": "NETWORK", "baudrate": None, "printerProfile": "_default"}, "options": {"ports": ["NETWORK"], "baudrates": [], "printerProfiles": [{"id": "_default", "name": "FlashForge Finder"}]}})
            elif path == "/api/printer":
                snap = self.bridge.snapshot()
                temp = snap["temperatures"]
                machine = snap["machine"]
                flags = {
                    "operational": machine not in ("OFFLINE", "ERROR"),
                    "printing": machine == "BUILDING_FROM_SD" or snap["job"].get("phase") == "PRINTING",
                    "paused": machine == "PAUSED",
                    "cancelling": False,
                    "pausing": False,
                    "error": machine == "ERROR" or snap["job"].get("phase") == "ERROR",
                    "ready": machine == "READY",
                    "closedOrError": machine in ("OFFLINE", "ERROR"),
                }
                self.send_json({"temperature": temp, "state": {"text": machine, "flags": flags}})
            elif path == "/api/job":
                snap = self.bridge.snapshot()
                progress = snap["progress"]
                job = snap["job"]
                started_at = job.get("started_at")
                self.send_json({
                    "job": {"file": {"name": job.get("filename"), "origin": "local", "size": job.get("filesize")}},
                    "progress": {
                        "completion": progress.get("completion"),
                        "filepos": progress.get("current"),
                        "printTime": int(time.time() - started_at) if started_at else None,
                        "printTimeLeft": None,
                    },
                    "state": snap["machine"],
                })
            elif path in ("/api/files", "/api/files/local"):
                with self.bridge.state_lock:
                    job = asdict(self.bridge.state)
                files = []
                if job.get("filename"):
                    files.append({"name": job["filename"], "path": job["filename"], "origin": "local", "size": job.get("filesize"), "date": int(job.get("queued_at") or time.time())})
                self.send_json({"files": files, "free": 0, "total": 0})
            elif path == "/":
                self.send_html(self.ui_html())
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            with self.bridge.state_lock:
                self.bridge.state.last_error = str(exc)
            self.send_json({"error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/files/local":
                self.handle_upload()
            elif path == "/api/job":
                body = self.read_body()
                data = json.loads(body.decode("utf-8") or "{}")
                self.bridge.job_command(str(data.get("command", "")), data.get("action"))
                self.send_json({}, HTTPStatus.NO_CONTENT)
            elif path == "/api/connection":
                # Orca may issue a no-op connect command after testing the endpoint.
                self.read_body()
                self.send_json({}, HTTPStatus.NO_CONTENT)
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, GXError, FinderProtocolError, OSError, json.JSONDecodeError) as exc:
            with self.bridge.state_lock:
                self.bridge.state.last_error = str(exc)
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path.startswith("/api/files/local/"):
            self.send_json({}, HTTPStatus.NO_CONTENT)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024 * 1024:
            raise ValueError("Invalid Content-Length")
        return self.rfile.read(length)

    def handle_upload(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise ValueError("Expected multipart/form-data")
        body = self.read_body()
        envelope = (f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + body
        msg = BytesParser(policy=email_policy).parsebytes(envelope)
        upload_name = None
        upload_bytes = None
        fields: dict[str, str] = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                upload_name = Path(filename).name
                upload_bytes = payload
            elif name:
                fields[name] = payload.decode("utf-8", "replace").strip()
        if upload_name is None or upload_bytes is None:
            raise ValueError("No file field in upload")

        gx_path, remote_name, info, converted = self.bridge.prepare_upload(upload_name, upload_bytes)
        print_now = fields.get("print", "false").lower() in ("1", "true", "yes")
        remote = self.bridge.queue_upload(gx_path, remote_name, print_now=print_now)
        print(
            f"Accepted Orca upload: {upload_name} -> {remote_name}; "
            f"converted={converted}; GX size={info['size']:,}; print={print_now}",
            flush=True,
        )
        self.send_json({
            "done": True,
            "files": {"local": {"name": remote_name, "origin": "local", "path": remote_name, "size": info["size"]}},
            "remote": remote,
            "converted_to_gx": converted,
        }, HTTPStatus.CREATED)

    def ui_html(self) -> str:
        return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>FlashForge Finder</title><style>
body{{font:16px system-ui;margin:24px;max-width:900px}}button{{padding:8px 14px;margin:4px}}pre{{background:#111;color:#ddd;padding:14px;white-space:pre-wrap;border-radius:8px}}
</style></head><body><h1>FlashForge Finder Orca Bridge {VERSION}</h1><p>Orca G-code is automatically converted to a validated Finder GX container before upload.</p><p id='summary'>Loading…</p><button id='refresh'>Refresh</button><button id='cancel'>Cancel print</button><pre id='data'></pre>
<script>
async function load(){{const r=await fetch('/api/job');const j=await r.json();const p=await (await fetch('/api/printer')).json();document.getElementById('summary').textContent=p.state.text+' — '+p.temperature.tool0.actual+' / '+p.temperature.tool0.target+' °C';document.getElementById('data').textContent=JSON.stringify({{job:j,printer:p}},null,2)}}
document.getElementById('refresh').addEventListener('click',load);document.getElementById('cancel').addEventListener('click',async()=>{{await fetch('/api/job',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{command:'cancel'}})}});load()}});load();setInterval(load,3000)
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="OctoPrint-compatible Orca bridge for FlashForge Finder")
    ap.add_argument("--printer", required=True, help="Finder IP address")
    ap.add_argument("--printer-port", type=int, default=8899)
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8898)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--spool", type=Path, default=Path(tempfile.gettempdir()) / "finder-orca-bridge")
    ap.add_argument("--version", action="version", version=f"finder_orca_bridge {VERSION}")
    args = ap.parse_args()

    bridge = FinderBridge(args.printer, args.printer_port, args.timeout, args.spool)
    server = BridgeHTTPServer((args.listen, args.port), Handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    print(f"Finder Orca Bridge {VERSION}")
    print(f"OctoPrint API identity: OctoPrint {OCTOPRINT_EMULATION_VERSION}")
    print("GX conversion and validation: enabled")
    print(f"Printer: {args.printer}:{args.printer_port}")
    print(f"Orca host: http://{args.listen}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
