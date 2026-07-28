#!/usr/bin/env python3
"""Multi-printer GUI manager for legacy FlashForge Finder printers."""
from __future__ import annotations

import ipaddress
import json
import queue
import re
import socket
import subprocess
import threading
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from finder_gx_v1_0 import build_gx, validate_gx
from finder_lan_v1_3 import FinderClient
from finder_orca_bridge_v0_3 import BridgeHTTPServer, FinderBridge, Handler

APP_VERSION = "0.5.0"
FINDER_PORT = 8899
FIRST_BRIDGE_PORT = 8898
CONFIG_DIR = Path.home() / ".finder-orca-bridge"
CONFIG_FILE = CONFIG_DIR / "printers.json"

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False
    DND_FILES = None
    TkinterDnD = None


@dataclass
class PrinterRecord:
    name: str
    ip: str
    bridge_port: int
    identity: str = "FlashForge Finder"
    enabled: bool = True


def decode(data: bytes) -> str:
    return data.replace(b"\x00", b"").decode("utf-8", "replace").strip()


def probe_finder(host: str, timeout: float = 0.45) -> dict | None:
    """Validate TCP/8899 and return Finder identity/status."""
    try:
        with FinderClient(host, FINDER_PORT, timeout=timeout) as printer:
            m115 = decode(printer.command("M115", timeout=max(0.8, timeout * 2)))
            try:
                m119 = decode(printer.command("M119", timeout=max(0.8, timeout * 2)))
            except Exception:
                m119 = ""
        identity_lines = [x.strip() for x in m115.splitlines() if x.strip() and x.strip().lower() != "ok"]
        identity = " | ".join(identity_lines[:3]) or "FlashForge Finder"
        status_match = re.search(r"^MachineStatus:\s*(.+)$", m119, re.MULTILINE)
        status = status_match.group(1).strip() if status_match else "ONLINE"
        return {"ip": host, "identity": identity, "status": status}
    except Exception:
        return None


def local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    """Find active private IPv4 /24 networks without external traffic."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        addresses.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    if subprocess.os.name == "nt":
        try:
            text = subprocess.check_output(["ipconfig"], text=True, errors="replace")
            addresses.update(re.findall(r"IPv4 Address[^:]*:\s*([0-9.]+)", text))
        except Exception:
            pass
    networks = {
        ipaddress.ip_network(f"{addr}/24", strict=False)
        for addr in addresses
        if not addr.startswith("127.") and ipaddress.ip_address(addr).is_private
    }
    return sorted(networks, key=lambda n: int(n.network_address))


class FinderGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"FlashForge Finder Orca Manager {APP_VERSION}")
        self.root.minsize(940, 690)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.files: list[Path] = []
        self.printers: list[PrinterRecord] = self.load_printers()
        self.discovered: dict[str, dict] = {}
        self.servers: dict[int, BridgeHTTPServer] = {}
        self.server_threads: dict[int, threading.Thread] = {}
        self.print_after_var = tk.BooleanVar(value=True)
        self.scan_status_var = tk.StringVar(value="Not scanned")
        self.status_var = tk.StringVar(value="Ready")
        self._build_ui()
        self.refresh_table()
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def load_printers(self) -> list[PrinterRecord]:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return [PrinterRecord(**item) for item in data.get("printers", [])]
        except Exception:
            return []

    def save_printers(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({"printers": [asdict(p) for p in self.printers]}, indent=2), encoding="utf-8")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        manager = ttk.LabelFrame(outer, text="Finder printers", padding=10)
        manager.pack(fill="both", expand=True)
        columns = ("name", "ip", "finder", "bridge", "status", "running")
        self.tree = ttk.Treeview(manager, columns=columns, show="headings", selectmode="extended", height=10)
        headings = {"name":"Printer name", "ip":"Finder LAN IP Address", "finder":"Finder port", "bridge":"Orca bridge URL", "status":"Printer status", "running":"Bridge"}
        widths = {"name":160, "ip":135, "finder":80, "bridge":170, "status":145, "running":80}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.edit_name())

        row = ttk.Frame(manager)
        row.pack(fill="x", pady=(8,0))
        ttk.Button(row, text="Scan local networks", command=self.scan_network).pack(side="left")
        ttk.Button(row, text="Add selected discovery", command=self.add_discovered).pack(side="left", padx=5)
        ttk.Button(row, text="Add IP manually", command=self.add_manual).pack(side="left")
        ttk.Button(row, text="Edit printer name", command=self.edit_name).pack(side="left", padx=5)
        ttk.Button(row, text="Edit IP / port", command=self.edit_connection).pack(side="left")
        ttk.Button(row, text="Remove", command=self.remove_selected_printers).pack(side="left", padx=5)
        ttk.Button(row, text="Start selected", command=self.start_selected).pack(side="right")
        ttk.Button(row, text="Stop selected", command=self.stop_selected).pack(side="right", padx=5)
        ttk.Button(row, text="Start all", command=self.start_all).pack(side="right")
        ttk.Label(manager, textvariable=self.scan_status_var).pack(anchor="w", pady=(6,0))

        files_box = ttk.LabelFrame(outer, text="Print files", padding=10)
        files_box.pack(fill="both", expand=True, pady=(10,0))
        self.drop_label = ttk.Label(files_box, text="Drop .gcode or .gx files here — or select files", anchor="center", relief="groove", padding=14)
        self.drop_label.pack(fill="x")
        if DND_AVAILABLE:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self._handle_drop)
        else:
            self.drop_label.configure(text="Select .gcode or .gx files below — optional drag-and-drop is not installed")
        controls = ttk.Frame(files_box)
        controls.pack(fill="x", pady=(8,6))
        ttk.Button(controls, text="Select files", command=self.select_files).pack(side="left")
        ttk.Button(controls, text="Remove selected", command=self.remove_selected_files).pack(side="left", padx=5)
        ttk.Button(controls, text="Clear", command=self.clear_files).pack(side="left")
        ttk.Checkbutton(controls, text="Start printing after upload", variable=self.print_after_var).pack(side="right")
        self.file_list = tk.Listbox(files_box, height=5, selectmode="extended")
        self.file_list.pack(fill="both", expand=True)
        ttk.Button(files_box, text="Convert and upload to selected printer", command=self.upload_to_selected).pack(anchor="w", pady=(8,0))

        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(8,2))
        self.log = tk.Text(outer, height=8, wrap="word", state="disabled")
        self.log.pack(fill="x")

    def selected_indexes(self) -> list[int]:
        return [int(item) for item in self.tree.selection() if str(item).isdigit()]

    def refresh_table(self) -> None:
        selected = set(self.selected_indexes())
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, p in enumerate(self.printers):
            found = self.discovered.get(p.ip)
            status = found["status"] if found else "Unknown"
            running = "Running" if p.bridge_port in self.servers else "Stopped"
            self.tree.insert("", "end", iid=str(i), values=(p.name, p.ip, FINDER_PORT, f"http://127.0.0.1:{p.bridge_port}", status, running))
        for i in selected:
            if i < len(self.printers): self.tree.selection_add(str(i))

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip()+"\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log": self._log(str(payload))
                elif kind == "status": self.status_var.set(str(payload))
                elif kind == "scan_done": self._apply_scan(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def scan_network(self) -> None:
        self.scan_status_var.set("Scanning…")
        self.status_var.set("Scanning local networks")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        networks = local_ipv4_networks()
        # Always revisit saved IPs even when they are outside the current /24.
        hosts = {p.ip for p in self.printers}
        for network in networks:
            hosts.update(str(ip) for ip in network.hosts())
        results = []
        with ThreadPoolExecutor(max_workers=96) as pool:
            futures = [pool.submit(probe_finder, host) for host in sorted(hosts, key=lambda x: tuple(int(y) for y in x.split('.')))]
            for future in as_completed(futures):
                result = future.result()
                if result: results.append(result)
        results.sort(key=lambda x: ipaddress.ip_address(x["ip"]))
        self.events.put(("scan_done", {"networks": [str(n) for n in networks], "results": results}))

    def _apply_scan(self, payload: dict) -> None:
        results: list[dict] = payload["results"]
        self.discovered = {r["ip"]: r for r in results}
        # Reconnect saved printers by unique identity when DHCP changed their IP.
        used = {p.ip for p in self.printers if p.ip in self.discovered}
        for p in self.printers:
            if p.ip in self.discovered: continue
            matches = [r for r in results if r["identity"] == p.identity and r["ip"] not in used]
            if len(matches) == 1:
                old = p.ip
                p.ip = matches[0]["ip"]
                used.add(p.ip)
                self._log(f"Rediscovered {p.name}: {old} → {p.ip}")
        self.save_printers()
        self.refresh_table()
        nets = ", ".join(payload["networks"]) or "saved addresses only"
        self.scan_status_var.set(f"Found {len(results)} Finder-compatible printer(s); scanned {nets}")
        self.status_var.set("Ready")
        known_ips = {p.ip for p in self.printers}
        new = [r for r in results if r["ip"] not in known_ips]
        for r in new:
            self._log(f"Possible Finder found at {r['ip']} — {r['identity']}")

    def next_bridge_port(self) -> int:
        used = {p.bridge_port for p in self.printers}
        port = FIRST_BRIDGE_PORT
        while port in used: port += 1
        return port

    def add_discovered(self) -> None:
        known = {p.ip for p in self.printers}
        candidates = [r for r in self.discovered.values() if r["ip"] not in known]
        if not candidates:
            messagebox.showinfo("No new printers", "No unadded Finder-compatible printers are currently discovered.")
            return
        for r in sorted(candidates, key=lambda x: ipaddress.ip_address(x["ip"])):
            self.printers.append(PrinterRecord(name=f"Finder {r['ip'].split('.')[-1]}", ip=r["ip"], bridge_port=self.next_bridge_port(), identity=r["identity"]))
        self.save_printers(); self.refresh_table()

    def add_manual(self) -> None:
        ip = simpledialog.askstring("Add Finder", "Finder IP address:", parent=self.root)
        if not ip: return
        try: ipaddress.ip_address(ip.strip())
        except ValueError:
            messagebox.showerror("Invalid IP", "Enter a valid IPv4 address."); return
        ip = ip.strip()
        if any(p.ip == ip for p in self.printers): return
        name = simpledialog.askstring("Printer name", "Editable printer name:", initialvalue=f"Finder {ip.split('.')[-1]}", parent=self.root) or f"Finder {ip.split('.')[-1]}"
        result = probe_finder(ip, 1.5)
        identity = result["identity"] if result else "FlashForge Finder"
        self.printers.append(PrinterRecord(name=name.strip(), ip=ip, bridge_port=self.next_bridge_port(), identity=identity))
        self.save_printers(); self.refresh_table()

    def one_selected(self) -> int | None:
        indexes = self.selected_indexes()
        if len(indexes) != 1:
            messagebox.showerror("Select one printer", "Select exactly one printer.")
            return None
        return indexes[0]

    def edit_name(self) -> None:
        i = self.one_selected()
        if i is None: return
        name = simpledialog.askstring("Edit printer name", "Printer name:", initialvalue=self.printers[i].name, parent=self.root)
        if name and name.strip():
            self.printers[i].name = name.strip(); self.save_printers(); self.refresh_table()

    def edit_connection(self) -> None:
        i = self.one_selected()
        if i is None: return
        p = self.printers[i]
        ip = simpledialog.askstring("Edit Finder IP", "Finder LAN IP address:", initialvalue=p.ip, parent=self.root)
        if not ip: return
        try: ipaddress.ip_address(ip.strip())
        except ValueError:
            messagebox.showerror("Invalid IP", "Enter a valid IPv4 address."); return
        port = simpledialog.askinteger("Edit bridge port", "Local Orca bridge port:", initialvalue=p.bridge_port, minvalue=1024, maxvalue=65535, parent=self.root)
        if port is None: return
        if any(x.bridge_port == port for j,x in enumerate(self.printers) if j != i):
            messagebox.showerror("Port in use", "Another saved printer already uses that bridge port."); return
        self.stop_printer(i); p.ip=ip.strip(); p.bridge_port=port; self.save_printers(); self.refresh_table()

    def remove_selected_printers(self) -> None:
        for i in sorted(self.selected_indexes(), reverse=True):
            self.stop_printer(i); del self.printers[i]
        self.save_printers(); self.refresh_table()

    def start_printer(self, i: int) -> None:
        p = self.printers[i]
        if p.bridge_port in self.servers: return
        try:
            spool = CONFIG_DIR / "spool" / re.sub(r"[^A-Za-z0-9._-]", "_", p.name)
            bridge = FinderBridge(p.ip, FINDER_PORT, 10.0, spool)
            server = BridgeHTTPServer(("127.0.0.1", p.bridge_port), Handler)
            server.bridge = bridge  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, name=f"finder-bridge-{p.bridge_port}", daemon=True)
            thread.start(); self.servers[p.bridge_port]=server; self.server_threads[p.bridge_port]=thread
            self._log(f"Started {p.name}: http://127.0.0.1:{p.bridge_port} → {p.ip}:{FINDER_PORT}")
        except Exception as exc:
            self._log(f"Could not start {p.name}: {exc}")

    def stop_printer(self, i: int) -> None:
        p = self.printers[i]
        server = self.servers.pop(p.bridge_port, None)
        self.server_threads.pop(p.bridge_port, None)
        if server:
            server.shutdown(); server.server_close(); self._log(f"Stopped {p.name}")

    def start_selected(self) -> None:
        for i in self.selected_indexes(): self.start_printer(i)
        self.refresh_table()

    def stop_selected(self) -> None:
        for i in self.selected_indexes(): self.stop_printer(i)
        self.refresh_table()

    def start_all(self) -> None:
        for i in range(len(self.printers)): self.start_printer(i)
        self.refresh_table()

    def _add_paths(self, paths: list[str]) -> None:
        for raw in paths:
            p=Path(raw)
            if p.is_file() and p.suffix.lower() in {".gcode",".gx",".g",".x3g"} and p not in self.files:
                self.files.append(p); self.file_list.insert("end", str(p))

    def _handle_drop(self,event) -> None: self._add_paths(list(self.root.tk.splitlist(event.data)))
    def select_files(self) -> None: self._add_paths(list(filedialog.askopenfilenames(filetypes=[("3D print files","*.gcode *.gx *.g *.x3g"),("All files","*.*")])))
    def remove_selected_files(self) -> None:
        for i in reversed(self.file_list.curselection()): self.file_list.delete(i); del self.files[i]
    def clear_files(self) -> None: self.files.clear(); self.file_list.delete(0,"end")

    def upload_to_selected(self) -> None:
        i=self.one_selected()
        if i is None: return
        selected=[self.files[x] for x in self.file_list.curselection()] or list(self.files)
        if not selected:
            messagebox.showerror("No files","Select at least one print file."); return
        threading.Thread(target=self._upload_worker,args=(self.printers[i],selected,self.print_after_var.get()),daemon=True).start()

    def _upload_worker(self,p:PrinterRecord,paths:list[Path],start_print:bool) -> None:
        for n,path in enumerate(paths,1):
            try:
                self.events.put(("status",f"Uploading {path.name} to {p.name}"))
                raw=path.read_bytes(); gx=build_gx(raw); info=validate_gx(gx)
                out=path if path.suffix.lower()==".gx" and raw==gx else path.with_suffix(".gx")
                if out != path: out.write_bytes(gx); self.events.put(("log",f"Created {out.name} ({info['size']:,} bytes)"))
                with FinderClient(p.ip,FINDER_PORT,timeout=10.0,finalize_delay=0.5) as printer:
                    remote=printer.upload(out,remote_name=out.name); self.events.put(("log",f"Uploaded to {p.name}: {remote}"))
                    if start_print: self.events.put(("log",decode(printer.start_print(remote))))
                if start_print and n < len(paths): break
            except Exception as exc:
                self.events.put(("log",f"Upload error for {p.name}: {exc}")); break
        self.events.put(("status","Ready"))

    def _on_close(self) -> None:
        for server in list(self.servers.values()): server.shutdown(); server.server_close()
        self.root.destroy()


def main() -> int:
    root=TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    FinderGUI(root); root.mainloop(); return 0

if __name__=="__main__": raise SystemExit(main())
