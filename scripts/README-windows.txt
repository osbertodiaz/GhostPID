GhostPID for Windows
==========================

Anonymizes P&IDs and PFDs in PDF before using them with LLMs. Everything is
processed locally: nothing leaves the computer.

Usage
-----
1. Unzip the zip into a folder you can write to (e.g.
   Documents\GhostPID). Nothing needs installing: OCR (tesseract, with
   English and Spanish) is included.
2. Open "GhostPID.exe". If Windows SmartScreen warns that the app is
   not signed, click "More info" and "Run anyway".
3. The first time, these are created next to the .exe:
   - data\     original PDFs, reports, maps and results (data\out\).
   - configs\  rules: base.yaml and example.yaml (not edited).
4. In the start dialog choose the PDF and your client's config, or create
   one with "New…" (it starts from the example, with fictitious names).
5. Review the detections, discard false positives, add terms and zones, and
   click "Confirm and generate". The anonymized PDF ends up in
   data\out\<document>.sanitized.pdf and the audit in data\reports\.

Important: data\ and configs\<client>.yaml contain the real names you want
to hide. Do not share them; share only the PDF in data\out\.

Language: the app is in Spanish if Windows is in Spanish and in English
otherwise. To force one, set the environment variable
GHOSTPID_LANG=en (or es).

License: GhostPID's code is MIT (LICENSE.txt). The app bundles third-party
components, including PyMuPDF (AGPL-3.0) and PyQt6 (GPL-3.0), so this
package is distributed under the GNU AGPL-3.0; the complete source code is at
the address below. Details and full texts are in the licenses\ folder and in
the app, under "About and licenses…".

Full manual: MANUAL.en.md (in this folder). Spanish: MANUAL.md.
Code and documentation: https://github.com/osbertodiaz/GhostPID
