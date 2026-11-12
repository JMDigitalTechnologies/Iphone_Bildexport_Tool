"""
iPhone Foto-Export per USB – sortiert nach Alben oder Personen.

Liest die Foto-Datenbank (Photos.sqlite) direkt vom iPhone über AFC
(pymobiledevice3) und kopiert die Originaldateien in Ordner pro Album
bzw. pro Person.

Voraussetzungen:
  - Windows: "Apple Mobile Device Support" (kommt mit iTunes oder der App "Apple Geräte")
  - pip install pymobiledevice3
  - iPhone entsperrt, per USB verbunden, "Diesem Computer vertrauen" bestätigt
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

APPLE_EPOCH = 978307200  # 2001-01-01 in Unix-Zeit
DB_DIR = "/PhotoData"
DB_FILES = ["Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"]
CHUNK = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Verbindung zum iPhone
# ---------------------------------------------------------------------------

class IPhone:
    """Synchroner Wrapper um das (asynchrone) pymobiledevice3.

    Alle Geräteaufrufe laufen in einer eigenen asyncio-Schleife in einem
    Hintergrund-Thread, damit GUI- und Export-Thread sie einfach aufrufen können.
    """

    def __init__(self):
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.afc import AfcService
        except ImportError:
            raise RuntimeError("pymobiledevice3 fehlt. Bitte 'pip install pymobiledevice3' ausführen.")
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="iphone-io").start()

        async def _connect():
            lockdown = await create_using_usbmux()
            afc = AfcService(lockdown=lockdown)
            await afc.connect()
            return lockdown, afc

        try:
            self.lockdown, self.afc = self._run(_connect())
        except Exception as e:
            self._loop.call_soon_threadsafe(self._loop.stop)
            raise RuntimeError(
                "Kein iPhone gefunden bzw. nicht vertraut.\n"
                "- iPhone per USB anstecken und entsperren\n"
                "- 'Diesem Computer vertrauen' bestätigen (evtl. Code eingeben)\n"
                "- iTunes oder 'Apple Geräte' muss installiert sein\n\n"
                f"Details: {type(e).__name__}: {e}"
            )
        values = getattr(self.lockdown, "all_values", None) or {}
        self.name = values.get("DeviceName") or "iPhone"
        self.ios = values.get("ProductVersion") or "?"

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def exists(self, path: str) -> bool:
        try:
            return bool(self._run(self.afc.exists(path)))
        except Exception:
            return False

    def listdir(self, path: str) -> list[str]:
        try:
            return [n for n in self._run(self.afc.listdir(path)) if n not in (".", "..")]
        except Exception:
            return []

    def download(self, src: str, dst: Path) -> int:
        """Datei stückweise kopieren (auch große Videos, ohne alles in den RAM zu laden)."""
        tmp = dst.with_name(dst.name + ".part")
        written = 0
        handle = self._run(self.afc.fopen(src, "r"))
        try:
            with open(tmp, "wb") as out:
                while True:
                    data = self._run(self.afc.fread(handle, CHUNK))
                    if not data:
                        break
                    out.write(data)
                    written += len(data)
        finally:
            try:
                self._run(self.afc.fclose(handle))
            except Exception:
                pass
        os.replace(tmp, dst)
        return written

    def read(self, path: str) -> bytes:
        """Kleine Datei (z.B. Vorschaubild) komplett lesen."""
        return self._run(self.afc.get_file_contents(path), timeout=30)


# ---------------------------------------------------------------------------
# Foto-Datenbank auswerten
# ---------------------------------------------------------------------------

@dataclass
class Asset:
    pk: int
    directory: str
    filename: str
    created: float | None
    adjusted: bool
    orig_name: str | None = None   # Originaldateiname (für Abgleich mit iCloud für Windows)
    orig_size: int | None = None   # Originalgröße in Bytes

    @property
    def path(self) -> str:
        return f"/{self.directory}/{self.filename}"


@dataclass
class Group:
    kind: str           # "album", "person" oder "month"
    key: int
    title: str
    asset_pks: list[int] = field(default_factory=list)
    folder: str = ""    # relativer Zielordner (leer = Standard aus kind + title)


MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni",
          "Juli", "August", "September", "Oktober", "November", "Dezember"]


class PhotoLibrary:
    def __init__(self, db_path: Path):
        self.con = sqlite3.connect(str(db_path))
        self.con.row_factory = sqlite3.Row
        tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.tables = tables
        self.asset_table = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"
        self.assets: dict[int, Asset] = self._load_assets()

    def _cols(self, table: str) -> list[str]:
        return [r[1] for r in self.con.execute(f"PRAGMA table_info({table})")]

    def _load_assets(self) -> dict[int, Asset]:
        cols = self._cols(self.asset_table)
        adj = "ZHASADJUSTMENTS" if "ZHASADJUSTMENTS" in cols else "0"
        where = "WHERE ZTRASHEDSTATE = 0" if "ZTRASHEDSTATE" in cols else ""
        sql = (f"SELECT Z_PK, ZDIRECTORY, ZFILENAME, ZDATECREATED, {adj} AS ADJ "
               f"FROM {self.asset_table} {where}")
        result = {}
        for r in self.con.execute(sql):
            if not r["ZDIRECTORY"] or not r["ZFILENAME"]:
                continue
            created = r["ZDATECREATED"] + APPLE_EPOCH if r["ZDATECREATED"] else None
            result[r["Z_PK"]] = Asset(r["Z_PK"], r["ZDIRECTORY"], r["ZFILENAME"], created, bool(r["ADJ"]))
        if "ZADDITIONALASSETATTRIBUTES" in self.tables:
            ac = self._cols("ZADDITIONALASSETATTRIBUTES")
            if {"ZASSET", "ZORIGINALFILENAME"} <= set(ac):
                size = "ZORIGINALFILESIZE" if "ZORIGINALFILESIZE" in ac else "NULL"
                for r in self.con.execute(f"SELECT ZASSET, ZORIGINALFILENAME, {size} "
                                          f"FROM ZADDITIONALASSETATTRIBUTES"):
                    a = result.get(r[0])
                    if a:
                        a.orig_name, a.orig_size = r[1], r[2]
        return result

    # --- Alben ---------------------------------------------------------------
    def _album_join(self):
        """Die Verknüpfungstabelle heißt je nach iOS-Version z.B. Z_28ASSETS / Z_30ASSETS."""
        for t in sorted(self.tables):
            if not re.fullmatch(r"Z_\d+ASSETS", t):
                continue
            cols = self._cols(t)
            album_col = next((c for c in cols if re.fullmatch(r"Z_\d+ALBUMS", c)), None)
            asset_col = next((c for c in cols if re.fullmatch(r"Z_\d+ASSETS", c)), None)
            if album_col and asset_col:
                return t, album_col, asset_col
        raise RuntimeError("Album-Tabelle in Photos.sqlite nicht gefunden (unbekannte iOS-Version?).")

    def albums(self) -> list[Group]:
        jt, album_col, asset_col = self._album_join()
        acols = self._cols("ZGENERICALBUM")
        trashed = "AND (a.ZTRASHEDSTATE = 0 OR a.ZTRASHEDSTATE IS NULL)" if "ZTRASHEDSTATE" in acols else ""
        sql = (f"SELECT a.Z_PK AS PK, a.ZTITLE AS TITLE, j.{asset_col} AS ASSET "
               f"FROM ZGENERICALBUM a JOIN {jt} j ON j.{album_col} = a.Z_PK "
               f"WHERE a.ZTITLE IS NOT NULL AND a.ZTITLE != '' {trashed}")
        groups: dict[int, Group] = {}
        for r in self.con.execute(sql):
            if r["ASSET"] not in self.assets:
                continue
            g = groups.setdefault(r["PK"], Group("album", r["PK"], r["TITLE"]))
            g.asset_pks.append(r["ASSET"])
        return sorted(groups.values(), key=lambda g: g.title.lower())

    # --- Personen ------------------------------------------------------------
    def people(self) -> list[Group]:
        if "ZPERSON" not in self.tables or "ZDETECTEDFACE" not in self.tables:
            return []
        fcols = self._cols("ZDETECTEDFACE")
        pcols = self._cols("ZPERSON")
        asset_col = "ZASSETFORFACE" if "ZASSETFORFACE" in fcols else "ZASSET"
        person_col = "ZPERSONFORFACE" if "ZPERSONFORFACE" in fcols else "ZPERSON"
        name_parts = [f"NULLIF(TRIM(p.{c}), '')" for c in ("ZDISPLAYNAME", "ZFULLNAME") if c in pcols]
        if not name_parts:
            return []
        name_expr = f"COALESCE({', '.join(name_parts)})"
        sql = (f"SELECT {name_expr} AS NAME, f.{asset_col} AS ASSET "
               f"FROM ZDETECTEDFACE f JOIN ZPERSON p ON p.Z_PK = f.{person_col} "
               f"WHERE {name_expr} IS NOT NULL")
        # Gleichnamige Personen (z.B. zusammengeführte) als eine Person behandeln
        groups: dict[str, Group] = {}
        for r in self.con.execute(sql):
            if r["ASSET"] not in self.assets:
                continue
            name = r["NAME"]
            g = groups.setdefault(name.lower(), Group("person", len(groups), name))
            if r["ASSET"] not in g.asset_pks:
                g.asset_pks.append(r["ASSET"])
        return sorted(groups.values(), key=lambda g: g.title.lower())

    # --- Nach Datum ------------------------------------------------------------
    def months(self) -> list[Group]:
        """Alle Medien der Mediathek (DCIM) nach Aufnahmemonat, neueste zuerst.
        key = JJJJMM, 0 = ohne Datum."""
        groups: dict[int, Group] = {}
        for a in self.assets.values():
            if not a.directory.upper().startswith("DCIM"):
                continue  # z.B. geteilte Alben, liegen nicht in der Mediathek
            if a.created:
                t = time.localtime(a.created)
                key = t.tm_year * 100 + t.tm_mon
                title = f"{MONTHS[t.tm_mon - 1]} {t.tm_year}"
                folder = f"Nach Datum/{t.tm_year}/{t.tm_mon:02d} {MONTHS[t.tm_mon - 1]}"
            else:
                key, title, folder = 0, "Ohne Datum", "Nach Datum/Ohne Datum"
            g = groups.setdefault(key, Group("month", key, title, folder=folder))
            g.asset_pks.append(a.pk)
        for g in groups.values():
            g.asset_pks.sort(key=lambda pk: self.assets[pk].created or 0)
        return sorted(groups.values(), key=lambda g: g.key, reverse=True)

    def close(self):
        self.con.close()


def load_library(phone: IPhone, workdir: Path) -> PhotoLibrary:
    if not phone.exists(f"{DB_DIR}/Photos.sqlite"):
        raise RuntimeError("Photos.sqlite wurde auf dem iPhone nicht gefunden.")
    for name in DB_FILES:
        src = f"{DB_DIR}/{name}"
        if phone.exists(src):
            phone.download(src, workdir / name)
    return PhotoLibrary(workdir / "Photos.sqlite")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name[:120] or "Ohne Namen"


ICLOUD_APP = r"shell:AppsFolder\AppleInc.iCloud_nzyj5cx40ttqa!iCloud"  # iCloud für Windows (Store-App)
ERROR_CLOUD_PROVIDER_NOT_RUNNING = 362  # Windows: „Der Clouddateianbieter wird nicht ausgeführt“


def find_icloud_photos_dir() -> Path | None:
    """Ordner von „iCloud Fotos“ aus iCloud für Windows suchen."""
    home = Path.home()
    for p in (home / "Pictures" / "iCloud Photos" / "Photos", home / "Pictures" / "iCloud Photos",
              home / "iCloudPhotos" / "Photos", home / "iCloudPhotos"):
        if p.is_dir():
            return p
    return None


class ICloudFolder:
    """Index der Dateien im iCloud-Fotos-Ordner von iCloud für Windows.

    Die Dateien sind dort oft nur Platzhalter; beim Lesen lädt Windows sie
    automatisch aus iCloud herunter. Der Index selbst liest nur Name und Größe
    und löst keinen Download aus.
    """

    def __init__(self, root: Path):
        self.root = root
        self.index: dict[str, list[tuple[Path, int]]] = {}
        self.count = 0
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.lower() == "desktop.ini":
                    continue
                p = Path(dirpath) / n
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                self.count += 1
                keys = {n.upper(), re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", n).upper()}  # IMG_1 (1).HEIC
                for k in keys:
                    self.index.setdefault(k, []).append((p, size))

    def find(self, names, size: int | None = None) -> Path | None:
        cands: list[tuple[Path, int]] = []
        for n in names:
            if n:
                cands += [c for c in self.index.get(n.upper(), []) if c not in cands]
        if not cands:
            return None
        if size:
            exact = [p for p, s in cands if s == size]
            if exact:
                return exact[0]
            return min(cands, key=lambda c: abs(c[1] - size))[0]
        return cands[0][0]


@dataclass
class Options:
    target: Path
    live_videos: bool = True
    edited: bool = False
    skip_existing: bool = True
    icloud: ICloudFolder | None = None   # fehlende Originale aus iCloud für Windows holen


class Exporter:
    def __init__(self, phone: IPhone, lib: PhotoLibrary, opts: Options, log, progress, stop_event):
        self.phone, self.lib, self.opts = phone, lib, opts
        self.log, self.progress, self.stop = log, progress, stop_event
        self._dircache: dict[str, list[str]] = {}
        self.stats: dict[str, int] = {}
        self.missing_examples: list[str] = []
        self.cancelled = False
        self._icloud_state: bool | None = None  # None = ungeprüft, True = bereit, False = übersprungen
        # Wartezustand, wenn iCloud für Windows nicht antwortet (für die Oberfläche)
        self.shared: dict = {}                   # wird vom WebApp mit dem Export-Status verknüpft
        self.resume_event = threading.Event()   # "Angemeldet – weiter" -> sofort neu prüfen
        self.skip_icloud = False                 # "Ohne iCloud fortfahren"
        self._probe_thread: threading.Thread | None = None
        self._probe_result: dict = {}

    def _listdir(self, path):
        if path not in self._dircache:
            self._dircache[path] = self.phone.listdir(path)
        return self._dircache[path]

    def _on_device(self, a: Asset) -> bool:
        names = {n.upper() for n in self._listdir(f"/{a.directory}")}
        return a.filename.upper() in names

    def _copy_local(self, src: Path, dst: Path, created: float | None):
        """Datei vom PC kopieren (bei iCloud-Platzhaltern lädt Windows sie dabei herunter)."""
        tmp = dst.with_name(dst.name + ".part")
        try:
            shutil.copyfile(src, tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dst)
        if created:
            os.utime(dst, (created, created))

    def _probe(self, path: Path, timeout: float) -> str:
        """1 Byte aus einer iCloud-Datei lesen, mit Zeitlimit (Lesen kann hängen, wenn iCloud wartet).
        Ergebnis: "ok", "not_running", "timeout" oder "error"."""
        if self._probe_thread is None or not self._probe_thread.is_alive():
            res = self._probe_result = {}

            def work():
                try:
                    with open(path, "rb") as f:
                        f.read(1)
                    res["r"] = "ok"
                except OSError as e:
                    res["r"] = ("not_running" if getattr(e, "winerror", None) == ERROR_CLOUD_PROVIDER_NOT_RUNNING
                                else "error")

            self._probe_thread = threading.Thread(target=work, daemon=True)
            self._probe_thread.start()
        self._probe_thread.join(timeout)
        return self._probe_result.get("r", "timeout")

    def _set_waiting(self, reason: str | None):
        self.shared["waiting"] = reason
        if reason:
            self.log(reason)

    def _ensure_icloud(self, sample: Path) -> bool:
        """Sicherstellen, dass iCloud für Windows Dateien liefert. Wartet sonst (mit Hinweis in der
        Oberfläche), bis iCloud bereit ist, der Nutzer "ohne iCloud" wählt oder abbricht."""
        if self._icloud_state is not None:
            return self._icloud_state
        r = self._probe(sample, 20)
        if r in ("ok", "error"):  # "error": iCloud antwortet, nur diese Datei ist problematisch
            self._icloud_state = True
            return True
        started = False
        if r == "not_running":
            try:
                os.startfile(ICLOUD_APP)
                started = True
            except OSError:
                pass
        self._set_waiting(
            ("iCloud für Windows wurde gestartet. " if started else "iCloud für Windows antwortet nicht. ")
            + "Bitte im iCloud-Fenster anmelden und „Fotos“ aktivieren. "
              "Der Export geht automatisch weiter, sobald iCloud bereit ist.")
        while not self.stop.is_set():
            if self.skip_icloud:
                self._set_waiting(None)
                self.log("Weiter ohne iCloud – fehlende Originale werden übersprungen.")
                self._icloud_state = False
                return False
            self.resume_event.wait(4)
            self.resume_event.clear()
            if self.stop.is_set() or self.skip_icloud:
                continue
            if self._probe(sample, 15) in ("ok", "error"):
                self._set_waiting(None)
                self.log("iCloud ist bereit – der Export läuft weiter.")
                self._icloud_state = True
                return True
        self._set_waiting(None)
        return False

    def _from_icloud(self, a: Asset, folder: Path) -> bool:
        """Original (und ggf. Live-Photo-Video) aus dem iCloud-Fotos-Ordner holen."""
        ic = self.opts.icloud
        if not ic:
            return False
        src = ic.find([a.orig_name, a.filename], a.orig_size)
        if not src:
            return False
        stem = os.path.splitext(a.filename)[0]
        items = [(src, folder / (stem + src.suffix))]
        if self.opts.live_videos and src.suffix.upper() in (".HEIC", ".JPG", ".JPEG"):
            mov = ic.find([os.path.splitext(n)[0] + ".MOV" for n in (a.orig_name, a.filename) if n])
            if mov:
                items.append((mov, folder / (stem + mov.suffix)))
        for s_path, dst in items:
            if self.opts.skip_existing and dst.exists() and dst.stat().st_size > 0:
                self.stats["skipped"] += 1
                continue
            if not self._ensure_icloud(s_path):
                return False  # ohne iCloud / abgebrochen -> als "nicht gefunden" zählen
            for attempt in (1, 2):
                try:
                    self._copy_local(s_path, dst, a.created)
                    self.stats["icloud"] += 1
                    break
                except OSError as e:
                    if attempt == 1 and getattr(e, "winerror", None) == ERROR_CLOUD_PROVIDER_NOT_RUNNING:
                        self._icloud_state = None  # iCloud wurde beendet -> erneut warten
                        if self._ensure_icloud(s_path):
                            continue
                        return False
                    self.stats["failed"] += 1
                    self.log(f"Fehler beim Laden aus iCloud ({s_path.name}): {e}")
                    break
        return True

    def _files_for(self, a: Asset) -> list[tuple[str, str]]:
        """(Quellpfad auf iPhone, Zieldateiname)"""
        files = [(a.path, a.filename)]
        stem, ext = os.path.splitext(a.filename)
        if self.opts.live_videos and ext.upper() in (".HEIC", ".JPG", ".JPEG"):
            for n in self._listdir(f"/{a.directory}"):
                s2, e2 = os.path.splitext(n)
                if s2 == stem and e2.upper() == ".MOV":
                    files.append((f"/{a.directory}/{n}", n))
        if self.opts.edited and a.adjusted:
            adj_dir = f"{DB_DIR}/Mutations/{a.directory}/{stem}/Adjustments"
            for n in self._listdir(adj_dir):
                if n.startswith("FullSizeRender"):
                    files.append((f"{adj_dir}/{n}", f"{stem}_bearbeitet{os.path.splitext(n)[1]}"))
        return files

    def run(self, groups: list[Group]):
        jobs = []
        for g in groups:
            if g.folder:
                folder = self.opts.target.joinpath(*(safe_name(p) for p in g.folder.split("/")))
            else:
                sub = "Alben" if g.kind == "album" else "Personen"
                folder = self.opts.target / sub / safe_name(g.title)
            for pk in g.asset_pks:
                jobs.append((folder, self.lib.assets[pk]))

        total = len(jobs)
        self.log(f"{total} Fotos/Videos in {len(groups)} Ordnern werden exportiert …")
        s = self.stats  # wird live aktualisiert (für die Fortschrittsanzeige)
        s.update(copied=0, icloud=0, skipped=0, missing=0, failed=0)
        self.missing_examples: list[str] = []
        self.cancelled = False
        if self.opts.icloud:
            self.log(f"Fehlende Originale werden aus „{self.opts.icloud.root}“ geholt "
                     f"({self.opts.icloud.count} Dateien).")

        for i, (folder, asset) in enumerate(jobs, 1):
            if self.stop.is_set():
                self.cancelled = True
                self.log("Abgebrochen.")
                break
            self.progress(i, total, asset.filename)
            folder.mkdir(parents=True, exist_ok=True)
            if not self._on_device(asset):
                # Original nicht auf dem iPhone ("iPhone-Speicher optimieren")
                main = folder / asset.filename
                if self.opts.skip_existing and main.exists() and main.stat().st_size > 0:
                    s["skipped"] += 1
                elif not self._from_icloud(asset, folder):
                    s["missing"] += 1
                    if len(self.missing_examples) < 5:
                        self.missing_examples.append(asset.orig_name or asset.filename)
                continue
            for src, dst_name in self._files_for(asset):
                dst = folder / dst_name
                if self.opts.skip_existing and dst.exists() and dst.stat().st_size > 0:
                    s["skipped"] += 1
                    continue
                try:
                    self.phone.download(src, dst)
                    if asset.created:
                        os.utime(dst, (asset.created, asset.created))
                    s["copied"] += 1
                except Exception as e:
                    if not self.phone.exists(src):
                        s["missing"] += 1
                        if len(self.missing_examples) < 5:
                            self.missing_examples.append(src)
                    else:
                        s["failed"] += 1
                        self.log(f"Fehler bei {src}: {e}")

        self.log(f"Fertig: {s['copied']} vom iPhone, {s['icloud']} aus iCloud, {s['skipped']} übersprungen "
                 f"(schon vorhanden), {s['missing']} nicht gefunden, {s['failed']} Fehler.")
        if s["missing"]:
            where = "weder auf dem iPhone noch im iCloud-Fotos-Ordner" if self.opts.icloud else "nicht auf dem iPhone"
            self.log(f"{s['missing']} Originale liegen {where}. Beispiele: " + ", ".join(self.missing_examples))
        self.log(f"Ziel: {self.opts.target}")
        return s


# ---------------------------------------------------------------------------
# Oberfläche: lokaler Webserver (nur 127.0.0.1) + ui.html im Browser
# ---------------------------------------------------------------------------

IDLE_TIMEOUT = 150  # Sekunden ohne offenen Browser-Tab -> Programm beendet sich


class WebApp:
    def __init__(self):
        self.lock = threading.Lock()
        self.phone: IPhone | None = None
        self.lib: PhotoLibrary | None = None
        self.groups: dict[str, Group] = {}
        self.phase = "idle"            # idle | connecting | ready | exporting
        self.error: str | None = None
        self.logs: list[str] = []
        self.export: dict | None = None
        self.stop_event = threading.Event()
        self.thumbs: dict[int, bytes | None] = {}
        self.last_seen = time.time()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="iphone_fotos_"))
        self.default_target = str(Path.home() / "Pictures" / "iPhone Export")
        self.icloud_dir = find_icloud_photos_dir()
        self.exporter: Exporter | None = None

    def log(self, msg: str):
        with self.lock:
            self.logs.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def touch(self):
        self.last_seen = time.time()

    # --- Verbinden ----------------------------------------------------------
    def start_connect(self):
        with self.lock:
            if self.phase in ("connecting", "exporting"):
                return False
            self.phase, self.error = "connecting", None
        threading.Thread(target=self._connect, daemon=True).start()
        return True

    def _connect(self):
        try:
            self.log("Verbinde mit iPhone …")
            phone = IPhone()
            self.log(f"Verbunden: {phone.name} (iOS {phone.ios}). Lade Foto-Datenbank …")
            if self.lib:
                self.lib.close()
            lib = load_library(phone, self.tmpdir)
            groups = {}
            for g in lib.albums():
                groups[f"a{g.key}"] = g
            for i, g in enumerate(lib.people()):
                groups[f"p{i}"] = g
            for g in lib.months():
                groups[f"m{g.key}"] = g
            with self.lock:
                self.phone, self.lib, self.groups = phone, lib, groups
                self.thumbs.clear()
                self.phase = "ready"
            self.log(f"Datenbank geladen: {len(lib.assets)} Medien.")
        except Exception as e:
            with self.lock:
                self.phase = "ready" if self.lib else "idle"
                self.error = str(e)
            self.log(f"Fehler: {e}")

    # --- Daten fürs UI -------------------------------------------------------
    def status(self, since: int) -> dict:
        with self.lock:
            dev = None
            if self.phone and self.lib:
                dev = {"name": self.phone.name, "ios": self.phone.ios, "media": len(self.lib.assets)}
            return {
                "phase": self.phase, "error": self.error, "device": dev,
                "logs": self.logs[since:], "logIndex": len(self.logs),
                "export": self.export, "defaultTarget": self.default_target,
                "icloudDir": str(self.icloud_dir) if self.icloud_dir else None,
            }

    def library(self) -> dict:
        out = {"albums": [], "people": [], "months": []}
        if not self.lib:
            return out
        bucket = {"album": "albums", "person": "people", "month": "months"}
        for gid, g in self.groups.items():
            assets = [self.lib.assets[pk] for pk in g.asset_pks]
            cover = max(assets, key=lambda a: a.created or 0).pk if assets else None
            item = {"id": gid, "title": g.title, "count": len(assets), "cover": cover}
            if g.kind == "month":
                item["year"] = g.key // 100 or None
                item["label"] = MONTHS[g.key % 100 - 1] if g.key else "Ohne Datum"
            out[bucket[g.kind]].append(item)
        return out

    def thumb(self, pk: int) -> bytes | None:
        if pk in self.thumbs:
            return self.thumbs[pk]
        a = self.lib.assets.get(pk) if self.lib else None
        data = None
        if a and self.phone:
            base = f"{DB_DIR}/Thumbnails/V2/{a.directory}/{a.filename}"
            names = ["5005.JPG"] + [n for n in self.phone.listdir(base) if n.upper().endswith(".JPG")]
            for n in names:
                try:
                    data = self.phone.read(f"{base}/{n}")
                    if data:
                        break
                except Exception:
                    continue
        if len(self.thumbs) > 2000:
            self.thumbs.clear()
        self.thumbs[pk] = data
        return data

    # --- Export ---------------------------------------------------------------
    def start_export(self, body: dict) -> str | None:
        groups = [self.groups[i] for i in body.get("ids", []) if i in self.groups]
        if not groups:
            return "Bitte mindestens ein Album oder eine Person auswählen."
        target = Path(str(body.get("target") or self.default_target)).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Zielordner kann nicht angelegt werden: {e}"
        opts = Options(target, bool(body.get("live", True)), bool(body.get("edited", False)),
                       bool(body.get("skip", True)))
        use_icloud = bool(body.get("icloud", True)) and self.icloud_dir is not None
        with self.lock:
            if self.phase != "ready":
                return "Gerade nicht möglich (keine Verbindung oder Export läuft schon)."
            self.phase = "exporting"
            self.export = {"running": True, "i": 0, "total": 0, "name": "", "target": str(target),
                           "stats": {"copied": 0, "icloud": 0, "skipped": 0, "missing": 0, "failed": 0},
                           "cancelled": False, "error": None, "missingExamples": [], "icloudUsed": use_icloud,
                           "waiting": None}
        self.stop_event.clear()
        threading.Thread(target=self._export, args=(groups, opts, use_icloud), daemon=True).start()
        return None

    def _export(self, groups, opts, use_icloud=False):
        exp = self.export

        def progress(i, total, name):
            exp.update(i=i, total=total, name=name)

        ex = Exporter(self.phone, self.lib, opts, self.log, progress, self.stop_event)
        ex.stats = exp["stats"]
        ex.shared = exp
        self.exporter = ex
        try:
            if use_icloud:
                exp["name"] = "Durchsuche iCloud-Fotos-Ordner …"
                opts.icloud = ICloudFolder(self.icloud_dir)
            ex.run(groups)
            exp["stats"] = ex.stats
            exp["cancelled"] = ex.cancelled
            exp["missingExamples"] = ex.missing_examples
        except Exception as e:
            exp["error"] = str(e)
            self.log(f"Fehler: {e}")
        with self.lock:
            exp["running"] = False
            self.phase = "ready"


def pick_folder(initial: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(initialdir=initial or str(Path.home()), title="Zielordner wählen",
                                       parent=root, mustexist=False)
    finally:
        root.destroy()
    return str(Path(path)) if path else None


def run_web(port: int = 0, token: str | None = None, open_browser: bool = True, app: WebApp | None = None):
    import json
    import secrets
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    token = token or secrets.token_urlsafe(18)
    html_file = Path(__file__).with_name("ui.html")
    app = app or WebApp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
                                 "script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _check(self, qs) -> bool:
            port_ = self.server.server_address[1]
            if self.headers.get("Host", "") not in (f"127.0.0.1:{port_}", f"localhost:{port_}"):
                self._send(403, "Forbidden", "text/plain")
                return False
            given = self.headers.get("X-Token") or qs.get("t", [""])[0]
            if not secrets.compare_digest(given, token):
                self._send(403, "Bitte das Tool über „iPhone Fotos starten.bat“ öffnen.",
                           "text/plain; charset=utf-8")
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._check(qs):
                return
            if u.path == "/":
                html = html_file.read_text("utf-8").replace("__TOKEN__", token)
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/status":
                app.touch()
                return self._send(200, app.status(int(qs.get("since", ["0"])[0] or 0)))
            if u.path == "/api/library":
                return self._send(200, app.library())
            if u.path == "/api/thumb":
                try:
                    data = app.thumb(int(qs.get("id", ["-1"])[0]))
                except Exception:
                    data = None
                if not data:
                    return self._send(404, b"", "image/jpeg")
                ctype = "image/svg+xml" if data.lstrip()[:4] == b"<svg" else "image/jpeg"
                return self._send(200, data, ctype, "private, max-age=3600")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if not self._check(parse_qs(u.query)):
                return
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            if u.path == "/api/connect":
                app.start_connect()
                return self._send(200, {"ok": True})
            if u.path == "/api/export":
                err = app.start_export(body)
                return self._send(400 if err else 200, {"ok": not err, "error": err})
            if u.path == "/api/stop":
                app.stop_event.set()
                if app.exporter:
                    app.exporter.resume_event.set()
                return self._send(200, {"ok": True})
            if u.path == "/api/icloud":
                ex = app.exporter
                if ex:
                    if body.get("action") == "skip":
                        ex.skip_icloud = True
                    ex.resume_event.set()  # sofort neu prüfen
                return self._send(200, {"ok": True})
            if u.path == "/api/pick-folder":
                try:
                    return self._send(200, {"path": pick_folder(str(body.get("initial") or ""))})
                except Exception as e:
                    return self._send(200, {"path": None, "error": str(e)})
            if u.path == "/api/open-folder":
                p = Path(str(body.get("path") or ""))
                if p.is_dir():
                    os.startfile(str(p))
                    return self._send(200, {"ok": True})
                return self._send(400, {"ok": False, "error": "Ordner existiert nicht."})
            if u.path == "/api/bye":
                app.last_seen = time.time() - IDLE_TIMEOUT + 10  # nach Tab-Schließen bald beenden
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"

    def watchdog():
        while True:
            time.sleep(5)
            if app.phase in ("connecting", "exporting"):
                continue
            if time.time() - app.last_seen > IDLE_TIMEOUT:
                server.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    print(f"Oberfläche läuft auf {url}")
    if open_browser:
        webbrowser.open(url)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Kommandozeile (optional):  python iphone_fotos.py --list
#                            python iphone_fotos.py --album "Urlaub" --person "Anna" --ziel D:\Fotos
# ---------------------------------------------------------------------------

def run_cli(argv):
    import argparse
    p = argparse.ArgumentParser(description="iPhone-Fotos nach Alben/Personen/Datum exportieren")
    p.add_argument("--list", action="store_true", help="Alben, Personen und Monate anzeigen")
    p.add_argument("--album", action="append", default=[], help="Albumname (mehrfach möglich, 'ALLE' für alle)")
    p.add_argument("--person", action="append", default=[], help="Personenname (mehrfach möglich, 'ALLE' für alle)")
    p.add_argument("--datum", action="append", default=[],
                   help="Jahr (2025) oder Monat (2025-07), mehrfach möglich, 'ALLE' für alle")
    p.add_argument("--ziel", default=str(Path.home() / "Pictures" / "iPhone Export"))
    p.add_argument("--ohne-live", action="store_true", help="Live-Photo-Videos nicht mitnehmen")
    p.add_argument("--bearbeitet", action="store_true", help="Bearbeitete Versionen zusätzlich speichern")
    p.add_argument("--ohne-icloud", action="store_true",
                   help="Fehlende Originale NICHT aus dem iCloud-Fotos-Ordner (iCloud für Windows) holen")
    a = p.parse_args(argv)

    phone = IPhone()
    print(f"Verbunden: {phone.name} (iOS {phone.ios})")
    lib = load_library(phone, Path(tempfile.mkdtemp(prefix="iphone_fotos_")))
    albums, people, months = lib.albums(), lib.people(), lib.months()

    if a.list or not (a.album or a.person or a.datum):
        print(f"\nAlben ({len(albums)}):")
        for g in albums:
            print(f"  {g.title}  ({len(g.asset_pks)})")
        print(f"\nPersonen ({len(people)}):")
        for g in people:
            print(f"  {g.title}  ({len(g.asset_pks)})")
        print(f"\nMonate ({len(months)}):")
        for g in months:
            code = f"{g.key // 100}-{g.key % 100:02d}" if g.key else "-"
            print(f"  {code}  {g.title}  ({len(g.asset_pks)})")
        return

    def pick_dates(values):
        if any(v.upper() == "ALLE" for v in values):
            return months
        found = []
        for v in values:
            m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", v.strip())
            hits = [g for g in months if m and g.key // 100 == int(m[1])
                    and (m[2] is None or g.key % 100 == int(m[2]))]
            if not hits:
                print(f"Nicht gefunden: {v}")
            found += [g for g in hits if g not in found]
        return found

    def pick(groups, names):
        if any(n.upper() == "ALLE" for n in names):
            return groups
        wanted = {n.lower() for n in names}
        found = [g for g in groups if g.title.lower() in wanted]
        for n in wanted - {g.title.lower() for g in found}:
            print(f"Nicht gefunden: {n}")
        return found

    groups = pick(albums, a.album) + pick(people, a.person) + pick_dates(a.datum)
    opts = Options(Path(a.ziel), not a.ohne_live, a.bearbeitet, True)
    icloud_dir = None if a.ohne_icloud else find_icloud_photos_dir()
    if icloud_dir:
        opts.icloud = ICloudFolder(icloud_dir)

    def progress(i, total, name):
        print(f"\r{i}/{total} {name:<30}", end="", flush=True)

    Exporter(phone, lib, opts, lambda m: print("\n" + m), progress, threading.Event()).run(groups)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            run_cli(sys.argv[1:])
        except RuntimeError as e:
            print(f"\n{e}")
            sys.exit(1)
    else:
        try:
            run_web()
        except KeyboardInterrupt:
            print("Beendet.")
        except Exception:
            # pythonw hat keine Konsole -> Fehler in Datei schreiben
            Path(__file__).with_name("iphone_fotos_fehler.log").write_text(traceback.format_exc(), "utf-8")
            raise
