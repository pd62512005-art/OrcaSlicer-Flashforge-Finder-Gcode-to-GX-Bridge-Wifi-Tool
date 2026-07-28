My Finder is currently hitting the bed. This may be an issue with offsets in orca. or it may be a bug in the conversion to a .gx file.


I am very new to this. It is buggy but working to some extent now

FlashForge Finder Orca Bridge v0.5.0 — Multi-Printer Manager

START
1. Extract the ZIP.
2. Run START_GUI.bat.
3. Click Scan local networks.
4. Click Add selected discovery. All newly detected Finder-compatible printers are added.
5. Double-click a printer row or use Edit printer name to assign a custom name.
6. Select printers and click Start selected, or click Start all.

MULTI-PRINTER MODEL
Each saved printer has:
- Editable printer name
- Finder LAN IP address and TCP port 8899
- Unique local Orca bridge port
- Status and bridge-running state

The first printer uses http://127.0.0.1:8898. Later printers use 8899, 8900, etc.
Configure one physical-printer entry in OrcaSlicer per saved Finder, using that printer's displayed local bridge URL and API key: finder.

DISCOVERY AND IP CHANGES
The scanner checks every active private local /24 plus every saved IP address. It validates the actual Finder protocol on TCP/8899 instead of treating any open port as a printer.

When a saved IP stops responding, the manager can update it automatically if exactly one newly discovered printer has the same M115 identity. Identical printers may return identical identity data; in that case automatic matching is ambiguous and the IP must be assigned manually.

FILES
Select or drop .gcode/.gx files, select one printer, then Convert and upload. Ordinary G-code is converted to Finder GX before upload.

OPTIONAL DRAG AND DROP
Run INSTALL_OPTIONAL_DRAG_DROP.bat. The rest of the program uses only Python's standard library.

CONFIGURATION
Saved printers are stored at:
%USERPROFILE%\.finder-orca-bridge\printers.json
