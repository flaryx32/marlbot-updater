# main.py – tiny launcher / self-updater for Marlbot
# --------------------------------------------------
# - checks GitHub for a new release
# - pulls every asset (exe/json/whatever) into the local Marlbot dir
# - shows changelog.json as HTML bullets
# - can fire up RLOrbital.exe when you’re ready
# 
# PySide6 GUI; tested on Win 11 + PyInstaller one-file build.
# --------------------------------------------------

import sys, os, subprocess, html, json as j, requests
from pathlib import Path
from PySide6.QtCore    import Qt, QThread, Signal
from PySide6.QtGui     import QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QPushButton, QFileDialog,
    QTextBrowser, QProgressBar, QMessageBox
)

import updater   # our helper with Github + download logic

# -------- cheap “config” ------ #
CFG_FILE    = Path.home() / ".marlbot_updater.json"
REPO        = "flaryx32/marlbot-updater"
PAT         = None                # leave None unless GH throttle hits you
ORBITAL_EXE = "RLOrbital.exe"

# -------- helper to find bundled files (PyInstaller) ---------- #
def res(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", Path(__file__).parent)  # _MEIPASS exists only in bundle
    return str(Path(base, rel))

# -------- quick json→html prettifier ------------------------- #
def json_to_html(obj) -> str:
    if isinstance(obj, dict):
        low = {k.lower(): v for k, v in obj.items()}
        out = []
        def bl(title, items):
            if isinstance(items, list) and items:
                li = "".join(f"<li>{html.escape(str(x))}</li>" for x in items)
                out.append(f"<h3>{title}</h3><ul>{li}</ul>")
        bl("Changes",      low.get("changes"))
        bl("Known issues", low.get("known_issues") or low.get("issues") or low.get("bugs"))
        if out:
            return "".join(out)
    # fallback: dump whole json
    return f"<pre>{html.escape(j.dumps(obj, indent=2))}</pre>"

# -------- tiny cfg helpers ----------------------------------- #
def load_cfg():
    if CFG_FILE.exists():
        return j.loads(CFG_FILE.read_text())
    return {"marlbot_dir": None, "current_version": "0.0.0"}

def save_cfg(cfg): CFG_FILE.write_text(j.dumps(cfg, indent=2))

# ==============================================================
# threaded downloader (keeps UI alive)
# ==============================================================

class DownloadThread(QThread):
    progress = Signal(int)
    done     = Signal(bool, str, object)      # ok, message, changelog-path/None

    def __init__(self, assets, target):
        super().__init__()
        self.assets, self.target = assets, target

    def run(self):
        try:
            cl = updater.download_all_assets(self.assets, self.target, self.progress.emit)
            self.done.emit(True, "Update completed ✔", cl)
        except Exception as e:
            self.done.emit(False, f"Update failed: {e}", None)

# ==============================================================
# main window
# ==============================================================

class Updater(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()

        # ----- basic window chrome -----
        self.setWindowTitle("Marlbot Updater")
        self.setWindowIcon(QIcon(res("favicon.ico")))
        self.resize(660, 560)

        # ----- widgets -----
        self.h1     = QLabel("<h2>Marlbot Updater</h2>", alignment=Qt.AlignCenter)
        self.status = QLabel(alignment=Qt.AlignCenter)
        self.notes  = QTextBrowser(); self.notes.setOpenExternalLinks(True)

        self.btn_upd = QPushButton("Update")
        self.btn_run = QPushButton("Launch RLOrbital")
        self.btn_cls = QPushButton("Close")
        self.btn_upd.setEnabled(False)
        self.btn_run.setEnabled(False)

        self.bar = QProgressBar(); self.bar.setVisible(False)

        # hook up signals
        self.btn_upd.clicked.connect(self.start_update)
        self.btn_run.clicked.connect(self.launch_orbital)
        self.btn_cls.clicked.connect(self.close)

        # layout
        lay = QVBoxLayout(self)
        for w in (self.h1, self.status, self.notes, self.bar,
                  self.btn_upd, self.btn_run, self.btn_cls):
            lay.addWidget(w, 1 if w is self.notes else 0)

        # style from qss
        self.setStyleSheet(open(res("ui.qss"), encoding="utf-8").read())

        # kick things off
        self.bootstrap()

    # ----------------------------------------------------------
    # first-run stuff
    # ----------------------------------------------------------
    def bootstrap(self):
        if not self.cfg["marlbot_dir"]:
            self.ask_for_folder()
        self.refresh_launch_btn()
        if self.cfg["marlbot_dir"]:
            self.check_github()
        else:
            self.status.setText("<b>No Marlbot folder selected.</b>")

    def ask_for_folder(self):
        default = Path(os.getenv("LOCALAPPDATA", "")) / "Marlbot"
        if default.exists():
            self.cfg["marlbot_dir"] = str(default); save_cfg(self.cfg); return
        dlg = QFileDialog(self, "Select your Marlbot folder"); dlg.setFileMode(QFileDialog.Directory)
        if dlg.exec():
            self.cfg["marlbot_dir"] = dlg.selectedFiles()[0]; save_cfg(self.cfg)

    # ----------------------------------------------------------
    # find RLOrbital.exe (root / bin / Release)
    # ----------------------------------------------------------
    def find_orbital(self) -> Path | None:
        root = Path(self.cfg.get("marlbot_dir", ""))
        for p in (root/ORBITAL_EXE, root/"bin"/ORBITAL_EXE, root/"Release"/ORBITAL_EXE):
            if p.exists():
                return p
        return None

    def refresh_launch_btn(self):
        self.orbital = self.find_orbital()
        self.btn_run.setEnabled(bool(self.orbital))

    # ----------------------------------------------------------
    # GitHub check + changelog preview
    # ----------------------------------------------------------
    def check_github(self):
        self.status.setText("Checking GitHub…"); QApplication.processEvents()

        try:
            rel = updater.fetch_latest_release(REPO, PAT)
        except Exception as e:
            self.status.setText(f"GitHub error: {e}")
            return

        local  = self.cfg["current_version"]
        remote = rel["tag_name"];  title = rel.get("name", "")
        self.new_version = updater.best_version_string(remote, title)

        if updater.compare_versions(remote, local, title):
            self.status.setText(f"New version <b>{self.new_version}</b> available (you have {local}).")

            self.assets = [a for a in rel["assets"] if not a["name"].startswith("Source code")]
            if not self.assets:
                self.status.setText("Latest release has no downloadable assets."); return

            # quick preview of changelog.json if present
            cl = next((a for a in self.assets if a["name"].lower() == "changelog.json"), None)
            if cl:
                try:
                    raw = requests.get(cl["browser_download_url"], timeout=10).text
                    self.notes.setHtml(json_to_html(j.loads(raw)))
                except Exception:
                    self.notes.setPlainText("Unable to preview changelog.json.")
            else:
                self.notes.setMarkdown(rel.get("body", "*No changelog*"))

            self.btn_upd.setEnabled(True)
        else:
            self.status.setText(f"✅ You are up-to-date ({local}).")
            self.notes.clear(); self.btn_upd.setEnabled(False)

    # ----------------------------------------------------------
    # update logic
    # ----------------------------------------------------------
    def start_update(self):
        self.btn_upd.setEnabled(False); self.bar.setVisible(True); self.bar.setValue(0)
        worker = DownloadThread(self.assets, Path(self.cfg["marlbot_dir"]))
        worker.progress.connect(self.bar.setValue)
        worker.done.connect(self.finish_update)
        worker.start()
        self.worker = worker      # keep reference

    def finish_update(self, ok, msg, changelog_path):
        self.bar.setVisible(False)
        if ok:
            self.cfg["current_version"] = self.new_version; save_cfg(self.cfg)
            if changelog_path:
                try:
                    data = j.loads(Path(changelog_path).read_text(encoding="utf-8"))
                    self.notes.setHtml(json_to_html(data))
                except Exception as e:
                    self.notes.setPlainText(f"Couldn't parse changelog.json:\n{e}")
            QMessageBox.information(self, "Done", msg)
        else:
            QMessageBox.critical(self, "Error", msg)

        self.refresh_launch_btn()
        self.check_github()

    # ----------------------------------------------------------
    # launch the game
    # ----------------------------------------------------------
    def launch_orbital(self):
        if not self.orbital:
            QMessageBox.warning(self, "Not found", f"{ORBITAL_EXE} not found."); return
        try:
            subprocess.Popen([str(self.orbital)], cwd=self.orbital.parent)
            self.close()          # yank this line if you want updater to stay open
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", str(e))

# --------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = Updater(); win.show()
    sys.exit(app.exec())
