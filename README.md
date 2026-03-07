# PDF-Agenda-Minuets-for-GUC

Pulls PDFs related to council agendas and meeting minuets 

PDF Minutes/Agenda Case Finder (URL List) — Tkinter GUI
- Paste multiple index URLs (one per line)
- Crawls for PDF links / CivicPlus ViewFile links
- Downloads docs, extracts text with pdfplumber
- Detects case keywords + decision/tally near matches
- Writes results to CSV

This version includes:
 Thread-safe UI updates (no Tk freezes)
 Much better logging so it doesn't "look stuck"
 Retries + shorter connect/read timeouts
 Progress bar uses per-seed doc count
 Cancel stops after current doc

Dependencies:
  pip install requests beautifulsoup4 lxml pdfplumber pillow

Packaging assets expected:
  assets/logo.png
  assets/app.ico (optional, for window icon)


#TODO
move worker settings out of Tk vars

add session + retries via Session()

add manifest/cache to skip already processed PDFs

dedupe case hits better

split core logic from GUI

improve detection patterns and snippet extraction

add summary stats and better output usability
