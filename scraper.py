"""
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
"""

from __future__ import annotations

import os
import re
import csv
import time
import hashlib
import threading
import queue
import sys
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pdfplumber

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk


# ----------------------------
# App metadata
# ----------------------------
APP_NAME = "PDFCaseFinder"
APP_VERSION = "1.0.0"
APP_COMPANY = "Atom Forge, A Subsidiary of Ethics Optional, Inc"
APP_AUTHOR = "Alex"


# ----------------------------
# PyInstaller-safe resource loader
# ----------------------------
def resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).resolve().parent
    return str(base_path / relative_path)


# ----------------------------
# Constants / patterns
# ----------------------------
UA = {"User-Agent": "LaSalleResearchBot/1.0"}

CASE_PATTERNS = {
    "rezoning": re.compile(
        r"\b(rezon(e|ing)|map amendment|zoning (ordinance )?amendment|text amendment)\b",
        re.I,
    ),
    "variance": re.compile(r"\b(variance|zoning board of appeals|ZBA)\b", re.I),
    "annexation": re.compile(r"\b(annexation|pre[-\s]?annex|annexation agreement)\b", re.I),
    "incentive": re.compile(
        r"\b(TIF|tax increment|abatement|incentive|redevelopment agreement|RDA|enterprise zone|business district)\b",
        re.I,
    ),
}

DECISION_PAT = re.compile(r"\b(approved|denied|tabled|continued|carried|failed)\b", re.I)
TALLY_PAT = re.compile(r"\b(\d+)\s*[-–]\s*(\d+)\b")
DATE_PAT = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"(\d{1,2}),\s+(\d{4})\b",
    re.I,
)

PDF_EXTS = (".pdf",)
CIVICPLUS_VIEWFILE_MARKERS = ("/agendacenter/viewfile/", "/archivecenter/viewfile/")


# ----------------------------
# Core helpers
# ----------------------------
def fetch(url: str) -> requests.Response:
    """
    Network fetch with retries + shorter timeouts so the GUI doesn't feel "stuck".
    """
    timeout = (10, 30)  # (connect, read)
    last_err: Optional[Exception] = None

    for attempt in range(1, 4):  # 3 tries
        try:
            r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep(0.75 * attempt)

    assert last_err is not None
    raise last_err


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_tail(url: str) -> str:
    p = urlparse(url)
    tail = os.path.basename(p.path) or "document"
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", tail)[:80]
    return tail


def host_slug(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    netloc = re.sub(r"[^a-z0-9.-]+", "_", netloc)
    return netloc[:80] or "host"


def is_probably_pdf_url(url: str) -> bool:
    u = url.lower().split("?")[0]
    if u.endswith(PDF_EXTS):
        return True
    return any(marker in u for marker in CIVICPLUS_VIEWFILE_MARKERS)


def crawl_index_for_docs(index_url: str) -> List[str]:
    soup = BeautifulSoup(fetch(index_url).text, "lxml")

    links = set()
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        full = urljoin(index_url, href)
        if is_probably_pdf_url(full):
            links.add(full)

    return sorted(links)


def download_doc(data_dir: str, subdir: str, url: str) -> Dict[str, str]:
    content = fetch(url).content
    h = sha256_bytes(content)

    out_dir = os.path.join(data_dir, subdir)
    os.makedirs(out_dir, exist_ok=True)

    tail = safe_tail(url)
    if not tail.lower().endswith(".pdf"):
        tail = f"{tail}.pdf"

    path = os.path.join(out_dir, f"{h}_{tail}")

    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)

    return {"sha256": h, "local_path": path, "source_url": url}


def extract_text_from_pdf(path: str, max_pages: int = 200) -> str:
    texts: List[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:max_pages]:
            t = page.extract_text() or ""
            if t.strip():
                texts.append(t)
    return "\n".join(texts)


def guess_meeting_date(text: str) -> Optional[str]:
    m = DATE_PAT.search(text)
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}, {m.group(3)}"


def detect_cases(text: str) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for case_type, pat in CASE_PATTERNS.items():
        for m in pat.finditer(text):
            start = max(0, m.start() - 350)
            end = min(len(text), m.end() + 350)
            snippet = text[start:end].strip()

            dm = DECISION_PAT.search(snippet)
            tm = TALLY_PAT.search(snippet)

            results.append(
                {
                    "case_type": case_type,
                    "decision": (dm.group(1).lower() if dm else ""),
                    "tally": (f"{tm.group(1)}-{tm.group(2)}" if tm else ""),
                    "snippet": snippet,
                }
            )
    return results


def write_cases_header(path: str) -> None:
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "jurisdiction_id",
                "jurisdiction_name",
                "meeting_date_guess",
                "seed_url",
                "source_url",
                "local_path",
                "sha256",
                "case_type",
                "decision_guess",
                "tally_guess",
                "snippet",
            ]
        )


def append_case_rows(out_csv: str, base: Dict[str, str], cases: List[Dict[str, str]]) -> None:
    with open(out_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for c in cases:
            w.writerow(
                [
                    base["jurisdiction_id"],
                    base["jurisdiction_name"],
                    base.get("meeting_date_guess") or "",
                    base["seed_url"],
                    base["source_url"],
                    base["local_path"],
                    base["sha256"],
                    c["case_type"],
                    c.get("decision") or "",
                    c.get("tally") or "",
                    (c.get("snippet") or "")[:1200],
                ]
            )


def parse_url_list(text_blob: str) -> List[str]:
    urls: List[str] = []
    for line in text_blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)

    seen = set()
    out: List[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# ----------------------------
# GUI App
# ----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(f"{APP_NAME} (v{APP_VERSION})")
        self.geometry("980x740")

        # set window icon (optional)
        try:
            self.iconbitmap(resource_path("assets/app.ico"))
        except Exception:
            pass

        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()

        self.data_dir = tk.StringVar(value=os.path.abspath("data_docs"))
        self.out_cases = tk.StringVar(value=os.path.abspath("cases.csv"))
        self.jid = tk.StringVar(value="custom")
        self.jname = tk.StringVar(value="Custom List Run")
        self.split_by_host = tk.BooleanVar(value=True)

        self._build_ui()
        self.after(100, self._drain_logs)

    # ---- thread-safe UI helper ----
    def ui(self, fn, *args, **kwargs):
        self.after(0, lambda: fn(*args, **kwargs))

    # ---- About dialog ----
    def show_about(self):
        win = tk.Toplevel(self)
        win.title(f"About {APP_NAME}")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()

        frame = ttk.Frame(win, padding=15)
        frame.pack(fill="both", expand=True)

        # logo
        try:
            img = Image.open(resource_path("assets/logo.png"))
            MAX_H = 90
            ratio = MAX_H / img.height
            img = img.resize((int(img.width * ratio), MAX_H), Image.LANCZOS)
            about_logo = ImageTk.PhotoImage(img)
            lbl_logo = ttk.Label(frame, image=about_logo)
            lbl_logo.image = about_logo
            lbl_logo.grid(row=0, column=0, rowspan=3, padx=(0, 12), sticky="n")
        except Exception:
            pass

        ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 14, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text=f"Version {APP_VERSION}").grid(row=1, column=1, sticky="w")
        ttk.Label(frame, text=f"{APP_COMPANY} • Created by {APP_AUTHOR}").grid(row=2, column=1, sticky="w")

        ttk.Separator(frame).grid(row=3, column=0, columnspan=2, sticky="we", pady=12)
        ttk.Button(frame, text="OK", command=win.destroy).grid(row=4, column=0, columnspan=2)

        frame.columnconfigure(1, weight=1)

    def _build_ui(self):
        # Menu bar (Help → About)
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

        # ---------- HEADER ----------
        header = ttk.Frame(self, padding=(10, 6))
        header.pack(fill="x")

        # Load & resize logo (fixed height so it doesn't take over the UI)
        try:
            logo_path = resource_path("assets/logo.png")
            img = Image.open(logo_path)
            max_h = 80
            ratio = max_h / img.height
            img = img.resize((int(img.width * ratio), max_h), Image.LANCZOS)
            self.logo_img = ImageTk.PhotoImage(img)
            ttk.Label(header, image=self.logo_img).pack(side="left", padx=(0, 12))
        except Exception:
            self.logo_img = None

        text_frame = ttk.Frame(header)
        text_frame.pack(side="left", fill="x", expand=True)

        ttk.Label(
            text_frame,
            text="PDF Finder for Agenda & Minutes",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            text_frame,
            text=f"Created by {APP_AUTHOR} • {APP_COMPANY}",
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=(0, 6))

        # ---------- MAIN ----------
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        # Paths
        paths = ttk.LabelFrame(top, text="Paths", padding=10)
        paths.pack(fill="x")

        self._path_row(paths, 0, "Docs folder", self.data_dir, select_dir=True)
        self._path_row(paths, 1, "Output cases CSV", self.out_cases, select_file_save=True, filetypes=[("CSV", "*.csv")])

        # Metadata
        meta = ttk.LabelFrame(top, text="Run Metadata", padding=10)
        meta.pack(fill="x", pady=(10, 0))

        ttk.Label(meta, text="Jurisdiction ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(meta, textvariable=self.jid, width=28).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(meta, text="Jurisdiction Name").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(meta, textvariable=self.jname, width=45).grid(row=0, column=3, sticky="w", padx=8)

        ttk.Checkbutton(
            meta,
            text="Put shit in different folders per city/site",
            variable=self.split_by_host,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        meta.columnconfigure(3, weight=1)

        # URL list
        url_frame = ttk.LabelFrame(self, text="Index URLs (one per line)", padding=10)
        url_frame.pack(fill="both", expand=False, padx=10, pady=(10, 0))

        self.url_text = tk.Text(url_frame, height=8, wrap="none")
        self.url_text.pack(fill="both", expand=True)
        self.url_text.insert(
            "end",
            "https://www.cityofottawa.org/agendacenter\n"
            "https://lasallecountyil.gov/AgendaCenter\n"
            "# lines starting with # are ignored\n",
        )

        # Controls
        controls = ttk.Frame(self, padding=10)
        controls.pack(fill="x")

        self.run_btn = ttk.Button(controls, text="Run", command=self.on_run)
        self.run_btn.pack(side="left")

        self.cancel_btn = ttk.Button(controls, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(controls, mode="determinate", length=420)
        self.progress.pack(side="left", padx=12, fill="x", expand=True)

        # Log
        log_frame = ttk.LabelFrame(self, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.log_text = tk.Text(log_frame, height=18, wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _path_row(self, parent, row, label, var, select_dir=False, select_file_save=False, filetypes=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=var, width=82).grid(row=row, column=1, sticky="we", padx=8, pady=2)

        def browse():
            if select_dir:
                p = filedialog.askdirectory(initialdir=var.get() or os.getcwd())
            elif select_file_save:
                p = filedialog.asksaveasfilename(
                    initialdir=os.path.dirname(var.get() or os.getcwd()),
                    defaultextension=".csv",
                    filetypes=filetypes or [("All files", "*.*")],
                )
            else:
                p = ""

            if p:
                var.set(p)

        ttk.Button(parent, text="Browse", command=browse).grid(row=row, column=2, sticky="e")
        parent.columnconfigure(1, weight=1)

    def log(self, msg: str):
        self.log_q.put(msg)

    def _drain_logs(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain_logs)

    def on_cancel(self):
        self.cancel_flag.set()
        self.log("Cancel requested… will stop after current document finishes.")

    def on_run(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Running", "A run is already in progress.")
            return

        data_dir = self.data_dir.get().strip()
        out_cases = self.out_cases.get().strip()
        jid = self.jid.get().strip()
        jname = self.jname.get().strip()
        url_blob = self.url_text.get("1.0", "end").strip()

        if not data_dir:
            messagebox.showerror("Error", "Docs folder is required.")
            return
        if not out_cases:
            messagebox.showerror("Error", "Output cases CSV is required.")
            return
        if not jid or not jname:
            messagebox.showerror("Error", "Jurisdiction label fields are required.")
            return

        urls = parse_url_list(url_blob)
        if not urls:
            messagebox.showerror("Error", "Paste at least one index URL.")
            return

        # UI state (main thread)
        self.cancel_flag.clear()
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=1)

        self.worker = threading.Thread(target=self._run_worker, args=(urls,), daemon=True)
        self.worker.start()

    def _run_worker(self, seed_urls: List[str]):
        try:
            self.log("Worker started.")
            os.makedirs(self.data_dir.get().strip(), exist_ok=True)
            write_cases_header(self.out_cases.get().strip())

            jid = self.jid.get().strip()
            jname = self.jname.get().strip()
            split_by_host = bool(self.split_by_host.get())

            for seed in seed_urls:
                if self.cancel_flag.is_set():
                    break

                self.log(f"\nCrawling seed: {seed}")

                try:
                    doc_urls = crawl_index_for_docs(seed)
                    doc_urls = [u for u in doc_urls if is_probably_pdf_url(u)]
                except Exception as e:
                    self.log(f"Seed crawl failed: {e}")
                    continue

                self.log(f"Found {len(doc_urls)} candidate documents.")
                # progress is per-seed
                self.ui(self.progress.configure, maximum=max(1, len(doc_urls)), value=0)

                subdir = jid
                if split_by_host:
                    subdir = os.path.join(jid, host_slug(seed))

                for i, doc_url in enumerate(doc_urls, start=1):
                    if self.cancel_flag.is_set():
                        break

                    try:
                        self.log(f"[{i}/{len(doc_urls)}] Downloading: {doc_url}")
                        meta = download_doc(self.data_dir.get().strip(), subdir, doc_url)
                        self.log(f"Saved: {meta['local_path']}")

                        self.log("Extracting text…")
                        text = extract_text_from_pdf(meta["local_path"], max_pages=200)
                        self.log(f"Extracted {len(text):,} chars")

                        if not text.strip():
                            self.log("No extractable text; skipping.")
                            self.ui(self.progress.configure, value=i)
                            continue

                        meeting_date_guess = guess_meeting_date(text) or ""
                        cases = detect_cases(text)

                        if cases:
                            base = {
                                "jurisdiction_id": jid,
                                "jurisdiction_name": jname,
                                "meeting_date_guess": meeting_date_guess,
                                "seed_url": seed,
                                **meta,
                            }
                            append_case_rows(self.out_cases.get().strip(), base, cases)

                        self.log(f"Cases found: {len(cases)}")
                        time.sleep(0.1)

                    except Exception as e:
                        self.log(f"Failed doc: {doc_url} -> {e}")

                    self.ui(self.progress.configure, value=i)

            if self.cancel_flag.is_set():
                self.log("\nStopped (canceled).")
            else:
                self.log("\nDone.")
                self.log(f"- Output cases: {self.out_cases.get().strip()}")
                self.log(f"- Docs folder:  {self.data_dir.get().strip()}")

        finally:
            # restore UI safely
            self.ui(self.run_btn.configure, state="normal")
            self.ui(self.cancel_btn.configure, state="disabled")


if __name__ == "__main__":
    App().mainloop()