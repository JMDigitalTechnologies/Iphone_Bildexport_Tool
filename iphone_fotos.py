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
import plistlib
import queue
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

APPLE_EPOCH = 978307200  # 2001-01-01 in Unix-Zeit
DB_DIR = "/PhotoData"
DB_FILES = ["Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"]
CHUNK = 4 * 1024 * 1024
VIDEO_EXT = {".MOV", ".MP4", ".M4V", ".AVI", ".3GP"}
AFC_TIMEOUT = 60  # Sekunden pro iPhone-Befehl


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
            self.lockdown, self.afc = self._run(_connect(), timeout=180)  # Zeit für "Vertrauen"-Dialog
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

    def _run(self, coro, timeout=AFC_TIMEOUT):
        """Gerätebefehl ausführen; nach `timeout` Sekunden abbrechen statt ewig zu warten."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise TimeoutError("iPhone antwortet nicht (Zeitüberschreitung) – entsperrt und verbunden?")

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
            with open(tmp, "wb") as out:  # bei Fehler/Zeitüberschreitung bleibt keine Teildatei liegen (s.u.)
                while True:
                    data = self._run(self.afc.fread(handle, CHUNK))
                    if not data:
                        break
                    out.write(data)
                    written += len(data)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            try:
                self._run(self.afc.fclose(handle), timeout=10)
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
    kind: int | None = None        # ZKIND: 0 = Foto, 1 = Video
    place: dict | None = None      # Ort laut iPhone: city, state, country, iso

    @property
    def is_video(self) -> bool:
        if self.kind is not None:
            return self.kind == 1
        return os.path.splitext(self.filename)[1].upper() in VIDEO_EXT

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


def decode_place(blob: bytes) -> dict | None:
    """Ortsangabe aus ZREVERSELOCATIONDATA lesen (vom iPhone ermittelte Adresse, NSKeyedArchiver-plist).
    Ergebnis: {"city", "subArea", "state", "country", "iso"} – fehlende Teile fehlen."""
    try:
        objs = plistlib.loads(blob)["$objects"]
    except Exception:
        return None

    def get(o, key):
        v = o.get(key) if isinstance(o, dict) else None
        if isinstance(v, plistlib.UID):
            v = objs[v.data]
        return None if v == "$null" else v

    root = next((o for o in objs if isinstance(o, dict) and "postalAddress" in o), None)
    out: dict = {}
    addr = get(root, "postalAddress") if root else None
    if isinstance(addr, dict):
        for key, name in (("_city", "city"), ("_subAdministrativeArea", "subArea"), ("_state", "state"),
                          ("_country", "country"), ("_ISOCountryCode", "iso")):
            v = get(addr, key)
            if isinstance(v, str) and v.strip():
                out[name] = v.strip()
    if root and not out.get("city"):
        # Fallback: Ortsliste der Karte (placeType 4 = Stadt, 2 = Bundesland, 1 = Land)
        items = get(get(root, "mapItem") or {}, "sortedPlaceInfos")
        for ref in (get(items, "NS.objects") or []) if isinstance(items, dict) else []:
            info = objs[ref.data] if isinstance(ref, plistlib.UID) else None
            kind, name = get(info, "placeType"), get(info, "name")
            if isinstance(name, str):
                field = {4: "city", 2: "state", 1: "country"}.get(kind)
                if field and field not in out:
                    out[field] = name
    if root and "iso" not in out:
        iso = get(root, "countryCode")
        if isinstance(iso, str):
            out["iso"] = iso
    return out or None


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
        kind = "ZKIND" if "ZKIND" in cols else "NULL"
        where = "WHERE ZTRASHEDSTATE = 0" if "ZTRASHEDSTATE" in cols else ""
        sql = (f"SELECT Z_PK, ZDIRECTORY, ZFILENAME, ZDATECREATED, {adj} AS ADJ, {kind} AS KIND "
               f"FROM {self.asset_table} {where}")
        result = {}
        for r in self.con.execute(sql):
            if not r["ZDIRECTORY"] or not r["ZFILENAME"]:
                continue
            created = r["ZDATECREATED"] + APPLE_EPOCH if r["ZDATECREATED"] else None
            result[r["Z_PK"]] = Asset(r["Z_PK"], r["ZDIRECTORY"], r["ZFILENAME"], created, bool(r["ADJ"]),
                                      kind=r["KIND"])
        if "ZADDITIONALASSETATTRIBUTES" in self.tables:
            ac = self._cols("ZADDITIONALASSETATTRIBUTES")
            if {"ZASSET", "ZORIGINALFILENAME"} <= set(ac):
                size = "ZORIGINALFILESIZE" if "ZORIGINALFILESIZE" in ac else "NULL"
                geo = "ZREVERSELOCATIONDATA" if "ZREVERSELOCATIONDATA" in ac else "NULL"
                for r in self.con.execute(f"SELECT ZASSET, ZORIGINALFILENAME, {size}, {geo} "
                                          f"FROM ZADDITIONALASSETATTRIBUTES"):
                    a = result.get(r[0])
                    if a:
                        a.orig_name, a.orig_size = r[1], r[2]
                        if r[3]:
                            a.place = decode_place(r[3])
        isos = Counter(a.place["iso"] for a in result.values() if a.place and a.place.get("iso"))
        self.home_iso = isos.most_common(1)[0][0] if isos else None  # Heimatland = häufigstes Land
        return result

    def place_label(self, a: "Asset", level: str) -> str:
        """Ordnername für den Ort eines Fotos. level: "city", "state" oder "country"."""
        p = a.place or {}
        if level == "country":
            name = p.get("country")
        elif level == "state":
            name = p.get("state") or p.get("country")
        else:
            name = p.get("city") or p.get("subArea") or p.get("state") or p.get("country")
        if not name:
            return "Ohne Ort"
        if level != "country" and p.get("iso") and p.get("country") and p["iso"] != self.home_iso \
                and name != p["country"]:
            name = f"{name} ({p['country']})"  # Ausland: Land dazuschreiben
        return name

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


def make_workdir() -> Path:
    """Arbeitsordner für die Kopie der Foto-Datenbank. Wird beim Beenden gelöscht; Reste früherer
    Läufe (z.B. nach einem Absturz) werden beim Start aufgeräumt."""
    import atexit
    base = Path(tempfile.gettempdir())
    for old in base.glob("iphone_fotos_*"):
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > 6 * 3600:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    d = Path(tempfile.mkdtemp(prefix="iphone_fotos_"))
    atexit.register(shutil.rmtree, d, True)
    return d


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


ICLOUD_STALL_SECONDS = 120        # so lange darf iCloud ohne Daten bleiben (plus Zuschlag nach Größe)
ICLOUD_MIN_RATE = 128 * 1024      # Zuschlag: 1 s je 128 KB Dateigröße (große Videos laden erst komplett;
                                  # bei parallelen Downloads teilen sie sich die Bandbreite)
# Parallelität (gemessen: iCloud skaliert bis ~8 gleichzeitige Downloads, USB ist bei ~3-4 ausgereizt)
ICLOUD_WORKERS = 8
DEVICE_WORKERS = 3
CONVERT_WORKERS = max(1, min(8, (os.cpu_count() or 2) - 2))
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000


def is_cloud_only(path: Path) -> bool:
    """Liegt die Datei nur als iCloud-Platzhalter vor (noch nicht heruntergeladen)?"""
    try:
        return bool(path.stat().st_file_attributes & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    except (OSError, AttributeError):
        return False


def release_cloud_file(path: Path):
    """Von iCloud heruntergeladene Datei wieder auf "nur online" setzen, damit Laufwerk C: nicht vollläuft.
    (Entspricht "Speicherplatz freigeben" im Explorer-Kontextmenü.)"""
    if os.name != "nt":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    attrs = k32.GetFileAttributesW(str(path))
    if attrs == 0xFFFFFFFF:
        return
    k32.SetFileAttributesW(str(path), (attrs & ~FILE_ATTRIBUTE_PINNED) | FILE_ATTRIBUTE_UNPINNED)


class ICloudStalled(OSError):
    """iCloud für Windows hat eine Datei nicht geliefert (Zeitüberschreitung)."""


def cancel_thread_io(native_tid: int | None):
    """Blockierendes Lesen eines Threads abbrechen (Windows: CancelSynchronousIo)."""
    if not native_tid or os.name != "nt":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    THREAD_TERMINATE = 0x0001  # von CancelSynchronousIo verlangtes Zugriffsrecht
    h = k32.OpenThread(THREAD_TERMINATE, False, native_tid)
    if h:
        k32.CancelSynchronousIo(h)
        k32.CloseHandle(h)


def restart_icloud_photos():
    """Den Fotos-Dienst von iCloud für Windows beenden und iCloud neu starten (nur auf Knopfdruck)."""
    import subprocess
    subprocess.run(["taskkill", "/IM", "iCloudPhotos.exe", "/F"], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        os.startfile(ICLOUD_APP)
    except OSError:
        pass


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


# ---------------------------------------------------------------------------
# Live-Umwandlung von HEIC in andere Bildformate (Pillow + pillow-heif)
# ---------------------------------------------------------------------------

HEIC_EXT = {".HEIC", ".HEIF", ".HIF"}
# Schlüssel: (Pillow-Format, Dateiendung, Anzeigename, Qualitätsregler sinnvoll)
CONVERT_FORMATS = {
    "jpg": ("JPEG", ".jpg", "JPG", True),
    "png": ("PNG", ".png", "PNG (verlustfrei)", False),
    "webp": ("WEBP", ".webp", "WebP", True),
    "avif": ("AVIF", ".avif", "AVIF", True),
    "tiff": ("TIFF", ".tif", "TIFF (verlustfrei, ohne GPS)", False),
    "jp2": ("JPEG2000", ".jp2", "JPEG 2000", True),
    "bmp": ("BMP", ".bmp", "BMP", False),
    "gif": ("GIF", ".gif", "GIF (256 Farben)", False),
    "pdf": ("PDF", ".pdf", "PDF", False),
}
_converter_cache: dict | None = None


def converter_info() -> dict:
    """Welche Zielformate stehen zur Verfügung? {"formats": [...], "error": str|None}"""
    global _converter_cache
    if _converter_cache is None:
        try:
            import pillow_heif
            from PIL import Image
            pillow_heif.register_heif_opener()
            Image.init()
            fmts = [{"id": k, "name": v[2], "quality": v[3]} for k, v in CONVERT_FORMATS.items()
                    if v[0] in Image.SAVE]
            _converter_cache = {"formats": fmts, "error": None}
        except Exception as e:
            _converter_cache = {"formats": [], "error": f"Bildbibliothek fehlt ({e}). "
                                "Bitte 'pip install pillow pillow-heif' ausführen."}
    return _converter_cache


_install_state = {"running": False}


def install_converter(log) -> None:
    """Pillow + pillow-heif nachinstallieren (auf Knopfdruck in der Oberfläche) und ohne Neustart laden."""
    global _converter_cache
    import importlib
    import site
    import subprocess
    if _install_state["running"]:
        return
    _install_state["running"] = True
    try:
        log("Installiere Bildbibliothek (pillow, pillow-heif) …")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                            "pillow>=11.2", "pillow-heif>=0.22"], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            log("Installation fehlgeschlagen: " + " | ".join(tail))
            return
        importlib.invalidate_caches()
        try:
            site.addsitedir(site.getusersitepackages())  # falls pip in den Benutzer-Ordner installiert hat
        except Exception:
            pass
        _converter_cache = None
        info = converter_info()
        log("Bildbibliothek installiert – HEIC-Umwandlung ist verfügbar." if not info["error"]
            else "Installiert, aber noch nicht ladbar – bitte das Tool neu starten.")
    finally:
        _install_state["running"] = False


def convert_image(src: Path, dst: Path, fmt: str, quality: int = 90):
    """HEIC-Bild in `fmt` umwandeln. Die Drehung ist schon angewendet (pillow-heif);
    EXIF (Aufnahmedatum, GPS, Kamera) und Farbprofil werden übernommen, wo das Format es kann."""
    from PIL import Image
    converter_info()  # registriert den HEIC-Öffner
    pil = CONVERT_FORMATS[fmt][0]
    with Image.open(src) as im:
        im.load()
        exif = im.getexif()
        icc = im.info.get("icc_profile")
        xmp = im.info.get("xmp")
        img = im
        if pil in ("JPEG", "BMP", "PDF", "JPEG2000") and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if pil == "GIF":
            img = img.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=256)
        kw: dict = {}
        if pil == "JPEG":
            kw.update(quality=quality, optimize=True, subsampling=0 if quality >= 90 else 2)
        elif pil in ("WEBP", "AVIF"):
            kw["quality"] = quality
        elif pil == "TIFF":
            kw["compression"] = "tiff_lzw"
        elif pil == "JPEG2000":
            kw.update(irreversible=True, quality_mode="rates", quality_layers=[max(1.0, (100 - quality) / 2)])
        elif pil == "PDF":
            kw["resolution"] = 72.0
        if exif and pil in ("JPEG", "PNG", "WEBP", "AVIF"):
            kw["exif"] = exif.tobytes()
        elif exif and pil == "TIFF":
            # TIFF verträgt die Unterblöcke (EXIF/GPS) des iPhones nicht -> Hauptdaten + Aufnahmedatum
            main = Image.Exif()
            for k, v in exif.items():
                if k not in (0x8769, 0x8825, 0x0112):
                    main[k] = v
            taken = exif.get_ifd(0x8769).get(0x9003)
            if taken:
                main[0x0132] = taken  # DateTime
            kw["exif"] = main
        if icc and pil in ("JPEG", "PNG", "WEBP", "AVIF", "TIFF"):
            kw["icc_profile"] = icc
        if xmp and pil in ("JPEG", "WEBP"):
            kw["xmp"] = xmp
        tmp = dst.with_name(dst.name + ".part")
        try:
            img.save(tmp, format=pil, **kw)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dst)


@dataclass
class Options:
    target: Path
    live_videos: bool = True
    edited: bool = False
    skip_existing: bool = True
    icloud: ICloudFolder | None = None   # fehlende Originale aus iCloud für Windows holen
    media: str = "all"                   # "all" = Fotos und Videos, "photos" = nur Fotos, "videos" = nur Videos
    convert: str | None = None           # HEIC live umwandeln in dieses Format (Schlüssel aus CONVERT_FORMATS)
    quality: int = 90                    # Qualität für JPG/WebP/AVIF/JPEG 2000
    keep_heic: bool = False              # HEIC-Original zusätzlich behalten
    place_level: str | None = None       # Unterordner nach Ort in Alben/Datum: "city", "state", "country"
    release_icloud: bool = True          # aus iCloud geladene Dateien danach wieder "nur online" (spart Platz auf C:)


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
        self.restart_icloud = False              # "iCloud neu starten"
        self.failed_icloud: list[str] = []       # Dateien, die iCloud nicht geliefert hat
        self._pool = None                        # Umwandlungs-Threads (nur bei aktiver Umwandlung)
        self._lock = threading.Lock()            # schützt Zähler/Listen (mehrere Threads)
        self._dir_lock = threading.Lock()
        self._dirsets: dict[str, set[str]] = {}  # Dateinamen je iPhone-Ordner (für _on_device)
        self._icloud_lock = threading.RLock()    # nur ein Thread prüft/wartet auf iCloud
        self._active: dict[int, dict] = {}       # laufende Übertragungen je Thread
        self._pool_slots: threading.Semaphore | None = None
        self._conv_lock = threading.Lock()
        self._probe_thread: threading.Thread | None = None
        self._probe_result: dict = {}

    # --- HEIC-Umwandlung ------------------------------------------------------
    def _final(self, dst: Path) -> Path:
        """Endgültiger Dateiname (bei Umwandlung mit neuer Endung)."""
        if self.opts.convert and dst.suffix.upper() in HEIC_EXT:
            return dst.with_suffix(CONVERT_FORMATS[self.opts.convert][1])
        return dst

    def _done(self, dst: Path) -> bool:
        """Liegt die Datei schon fertig im Ziel? (für "Vorhandene überspringen")"""
        if not self.opts.skip_existing:
            return False
        final = self._final(dst)
        ok = final.exists() and final.stat().st_size > 0
        if ok and final != dst and self.opts.keep_heic:
            ok = dst.exists() and dst.stat().st_size > 0
        return ok

    def _finish(self, dst: Path, created: float | None):
        """Nach dem Kopieren: Datum setzen und ggf. HEIC umwandeln – parallel in eigenen
        Threads, damit die Übertragung währenddessen weiterläuft."""
        if created:
            os.utime(dst, (created, created))
        final = self._final(dst)
        if final == dst:
            return
        with self._lock:  # mehrere Download-Threads können gleichzeitig hier ankommen
            if self._pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._pool = ThreadPoolExecutor(max_workers=CONVERT_WORKERS, thread_name_prefix="convert")
                self._pool_slots = threading.Semaphore(CONVERT_WORKERS * 2)  # Warteschlange begrenzt (Speicher)
        self._pool_slots.acquire()

        def job():
            try:
                convert_image(dst, final, self.opts.convert, self.opts.quality)
                if created:
                    os.utime(final, (created, created))
                if not self.opts.keep_heic:
                    dst.unlink(missing_ok=True)
                self._inc("converted")
            except Exception as e:
                self._inc("convFailed")
                self.log(f"Umwandeln fehlgeschlagen ({dst.name}): {e} – HEIC-Original bleibt erhalten.")
            finally:
                self._pool_slots.release()

        self._pool.submit(job)

    def _inc(self, key: str, n: int = 1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def _set_current(self, name: str | None, source: str = ""):
        """Laufende Übertragung dieses Threads merken (für die Anzeige: älteste zuerst)."""
        tid = threading.get_ident()
        with self._lock:
            if name is None:
                self._active.pop(tid, None)
            else:
                self._active[tid] = {"name": name, "source": source, "since": time.time()}
            act = sorted(self._active.values(), key=lambda c: c["since"])
            self.shared["current"] = act[0] if act else None
            self.shared["active"] = len(act)

    def _listdir(self, path):
        with self._dir_lock:
            if path in self._dircache:
                return self._dircache[path]
        names = self.phone.listdir(path)
        with self._dir_lock:
            if names:
                self._dircache[path] = names
        return names

    def _on_device(self, a: Asset) -> bool:
        d = f"/{a.directory}"
        names = self._dirsets.get(d)
        if names is None:
            listing = self._listdir(d)
            names = {n.upper() for n in listing}
            if listing:  # leer = Fehler oder leer -> nicht merken, später neu fragen
                self._dirsets[d] = names
        return a.filename.upper() in names

    def _copy_local(self, src: Path, dst: Path, created: float | None):
        """Datei vom PC kopieren (bei iCloud-Platzhaltern lädt Windows sie dabei herunter).

        Läuft in einem eigenen Thread mit Hänger-Erkennung: Kommen zu lange keine Daten,
        wird das Lesen über Windows abgebrochen (CancelSynchronousIo), statt ewig zu warten."""
        tmp = dst.with_name(dst.name + ".part")
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        stall_limit = ICLOUD_STALL_SECONDS + size / ICLOUD_MIN_RATE  # große Videos dürfen länger laden
        st = {"bytes": 0, "done": False, "err": None, "tid": None}

        def work():
            st["tid"] = threading.get_native_id()
            try:
                with open(src, "rb") as fi, open(tmp, "wb") as fo:
                    while True:
                        b = fi.read(1 << 20)
                        if not b:
                            break
                        fo.write(b)
                        st["bytes"] += len(b)
            except BaseException as e:
                st["err"] = e
            finally:
                st["done"] = True

        th = threading.Thread(target=work, daemon=True, name="icloud-copy")
        th.start()
        last, last_t, stalled = -1, time.time(), False
        while not st["done"]:
            th.join(1)
            if st["bytes"] != last:
                last, last_t = st["bytes"], time.time()
            elif self.stop.is_set() or time.time() - last_t > stall_limit:
                stalled = not self.stop.is_set()
                cancel_thread_io(st["tid"])  # blockiertes Lesen abbrechen
                th.join(15)
                break
        if not st["done"] or stalled or st["err"]:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if stalled or not st["done"]:
                raise ICloudStalled(f"iCloud hat {src.name} nach {int(stall_limit)} s nicht geliefert")
            raise st["err"]
        os.replace(tmp, dst)
        if created:
            os.utime(dst, (created, created))

    def _probe(self, path: Path, timeout: float) -> str:
        """1 Byte aus einer iCloud-Datei lesen, mit Zeitlimit (Lesen kann hängen, wenn iCloud wartet).
        Ergebnis: "ok", "not_running", "timeout" oder "error"."""
        if self._probe_thread is None or not self._probe_thread.is_alive():
            res = self._probe_result = {}

            def work():
                res["tid"] = threading.get_native_id()
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
        if self._probe_thread.is_alive():
            cancel_thread_io(self._probe_result.get("tid"))  # hängenden Test-Lesezugriff abbrechen
            self._probe_thread.join(5)
            return "timeout"
        return self._probe_result.get("r", "timeout")

    def _set_waiting(self, reason: str | None):
        self.shared["waiting"] = reason
        if reason:
            self.log(reason)

    def _ensure_icloud(self, sample: Path) -> bool:
        """Sicherstellen, dass iCloud für Windows Dateien liefert. Wartet sonst (mit Hinweis in der
        Oberfläche), bis iCloud bereit ist, der Nutzer "ohne iCloud" wählt oder abbricht.
        Bei parallelen Downloads prüft/wartet nur ein Thread, die anderen warten auf sein Ergebnis."""
        if self._icloud_state is not None:
            return self._icloud_state
        with self._icloud_lock:
            if self._icloud_state is not None:
                return self._icloud_state
            return self._ensure_icloud_locked(sample)

    def _ensure_icloud_locked(self, sample: Path) -> bool:
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
            + "Bitte prüfen, ob du im iCloud-Fenster angemeldet bist und „Fotos“ aktiv ist – "
              "oder „iCloud neu starten“ wählen. Der Export geht automatisch weiter, sobald iCloud bereit ist.")
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
            if self.restart_icloud:
                self.restart_icloud = False
                self.log("iCloud für Windows wird neu gestartet …")
                restart_icloud_photos()
                time.sleep(8)
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
            if self._done(dst):
                self._inc("skipped")
                continue
            if not self._ensure_icloud(s_path):
                return False  # ohne iCloud / abgebrochen -> als "nicht gefunden" zählen
            self._set_current(s_path.name, "iCloud")
            was_cloud_only = is_cloud_only(s_path)
            for attempt in (1, 2):
                try:
                    self._copy_local(s_path, dst, None)
                    self._inc("bytes", dst.stat().st_size)
                    self._finish(dst, a.created)
                    self._inc("icloud")
                    if was_cloud_only and self.opts.release_icloud:
                        release_cloud_file(s_path)  # C: nicht volllaufen lassen
                    break
                except ICloudStalled as e:
                    # Datei auslassen (späterer Export holt sie nach); vor der nächsten Datei prüfen,
                    # ob iCloud überhaupt noch reagiert -> sonst Warte-Hinweis statt Hängen.
                    self._inc("failed")
                    self.failed_icloud.append(s_path.name)
                    self.log(f"{e} – übersprungen, wird beim nächsten Export erneut versucht.")
                    self._icloud_state = None
                    break
                except OSError as e:
                    if attempt == 1 and getattr(e, "winerror", None) == ERROR_CLOUD_PROVIDER_NOT_RUNNING:
                        self._icloud_state = None  # iCloud wurde beendet -> erneut warten
                        if self._ensure_icloud(s_path):
                            continue
                        return False
                    self._inc("failed")
                    self.failed_icloud.append(s_path.name)
                    self.log(f"Fehler beim Laden aus iCloud ({s_path.name}): {e}")
                    break
        return True

    def _job_icloud(self, folder: Path, asset: Asset):
        """Original liegt nicht auf dem iPhone ("iPhone-Speicher optimieren") -> iCloud-Ordner."""
        main = folder / asset.filename
        if self._done(main):
            self._inc("skipped")
        elif not self._from_icloud(asset, folder):
            self._inc("missing")
            with self._lock:
                if len(self.missing_examples) < 5:
                    self.missing_examples.append(asset.orig_name or asset.filename)

    def _job_device(self, folder: Path, asset: Asset):
        for src, dst_name in self._files_for(asset):
            dst = folder / dst_name
            if self._done(dst):
                self._inc("skipped")
                continue
            self._set_current(dst_name, "iPhone")
            try:
                n = self.phone.download(src, dst)
                self._inc("bytes", n or 0)
                self._finish(dst, asset.created)
                self._inc("copied")
            except Exception as e:
                if not self.phone.exists(src):
                    self._inc("missing")
                    with self._lock:
                        if len(self.missing_examples) < 5:
                            self.missing_examples.append(src)
                else:
                    self._inc("failed")
                    self.log(f"Fehler bei {src}: {e}")

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
            by_place = self.opts.place_level and g.kind in ("album", "month")
            for pk in g.asset_pks:
                a = self.lib.assets[pk]
                if (self.opts.media == "photos" and a.is_video) or (self.opts.media == "videos" and not a.is_video):
                    continue
                if by_place:
                    jobs.append((folder / safe_name(self.lib.place_label(a, self.opts.place_level)), a))
                else:
                    jobs.append((folder, a))

        total = len(jobs)
        what = {"photos": "Fotos (ohne Videos)", "videos": "Videos (ohne Fotos)"}.get(self.opts.media, "Fotos/Videos")
        if self.opts.place_level:
            lvl = {"city": "Stadt", "state": "Bundesland", "country": "Land"}[self.opts.place_level]
            self.log(f"Alben und Monate bekommen Unterordner nach Ort ({lvl}).")
        self.log(f"{total} {what} in {len(groups)} Ordnern werden exportiert …")
        s = self.stats  # wird live aktualisiert (für die Fortschrittsanzeige)
        s.update(copied=0, icloud=0, skipped=0, missing=0, failed=0, converted=0, convFailed=0, bytes=0)
        if self.opts.convert:
            fmt = CONVERT_FORMATS[self.opts.convert]
            self.log(f"HEIC-Bilder werden live in {fmt[2]} umgewandelt"
                     + (f" (Qualität {self.opts.quality})" if fmt[3] else "")
                     + (", HEIC-Original bleibt zusätzlich erhalten." if self.opts.keep_heic else "."))
        self.missing_examples: list[str] = []
        self.cancelled = False
        if self.opts.icloud:
            self.log(f"Fehlende Originale werden aus „{self.opts.icloud.root}“ geholt "
                     f"({self.opts.icloud.count} Dateien).")

        # Parallele Pipeline: iPhone (USB) und iCloud laufen gleichzeitig in eigenen Thread-Gruppen,
        # die Umwandlung in einer dritten. Die Warteschlange ist begrenzt, damit Fortschritt,
        # Abbrechen und Speicherbedarf überschaubar bleiben.
        from concurrent.futures import ThreadPoolExecutor
        self.log(f"Parallel: {DEVICE_WORKERS} × iPhone, {ICLOUD_WORKERS if self.opts.icloud else 0} × iCloud"
                 + (f", {CONVERT_WORKERS} × Umwandlung" if self.opts.convert else "") + ".")
        dev_pool = ThreadPoolExecutor(DEVICE_WORKERS, thread_name_prefix="iphone")
        ic_pool = ThreadPoolExecutor(ICLOUD_WORKERS, thread_name_prefix="icloud")
        slots = threading.Semaphore(DEVICE_WORKERS + ICLOUD_WORKERS + 4)
        done = [0]
        self.shared["startedAt"] = time.time()

        def finished(asset):
            with self._lock:
                done[0] += 1
                n = done[0]
            self.progress(n, total, asset.filename)
            slots.release()

        def job(folder, asset, on_device):
            try:
                if self.stop.is_set():
                    return
                folder.mkdir(parents=True, exist_ok=True)
                if on_device:
                    self._job_device(folder, asset)
                else:
                    self._job_icloud(folder, asset)
            except Exception as e:  # ein Fehler bei einer Datei darf den Export nicht stoppen
                self._inc("failed")
                self.log(f"Fehler bei {asset.filename}: {e}")
            finally:
                self._set_current(None)
                finished(asset)

        self.progress(0, total, "")
        # Fotos zuerst, große Videos zum Schluss: iCloud liefert große Dateien weitgehend nacheinander
        # und hält solange auch kleine zurück. So kommt das meiste früh an und die Restzeit stimmt besser.
        jobs.sort(key=lambda j: (j[1].is_video, j[1].orig_size or 0) if j[1].is_video else (False, 0))
        for folder, asset in jobs:
            while not slots.acquire(timeout=0.5):
                if self.stop.is_set():
                    break
            if self.stop.is_set():
                break
            on_device = self._on_device(asset)
            (dev_pool if on_device else ic_pool).submit(job, folder, asset, on_device)
        if self.stop.is_set():
            self.cancelled = True
            self.log("Abgebrochen – laufende Übertragungen werden beendet …")
        for pool in (dev_pool, ic_pool):
            pool.shutdown(wait=True, cancel_futures=self.stop.is_set())
        self.shared["active"] = 0

        if self._pool:
            self.progress(total, total, "Wandle die letzten Bilder um …")
            self._pool.shutdown(wait=True)
            self._pool = None
        if self.opts.convert:
            self.log(f"Umgewandelt: {s['converted']} Bilder"
                     + (f", {s['convFailed']} fehlgeschlagen (als HEIC behalten)" if s["convFailed"] else "") + ".")
        dur = max(1.0, time.time() - self.shared.get("startedAt", time.time()))
        self.log(f"Dauer {int(dur // 60)} min {int(dur % 60)} s, übertragen {s['bytes'] / 1e9:.2f} GB "
                 f"({s['bytes'] / 1e6 / dur:.1f} MB/s).")
        self.log(f"Fertig: {s['copied']} vom iPhone, {s['icloud']} aus iCloud, {s['skipped']} übersprungen "
                 f"(schon vorhanden), {s['missing']} nicht gefunden, {s['failed']} Fehler.")
        if s["missing"]:
            where = "weder auf dem iPhone noch im iCloud-Fotos-Ordner" if self.opts.icloud else "nicht auf dem iPhone"
            self.log(f"{s['missing']} Originale liegen {where}. Beispiele: " + ", ".join(self.missing_examples))
        if self.failed_icloud:
            self.log(f"{len(self.failed_icloud)} Dateien hat iCloud nicht geliefert (z.B. "
                     + ", ".join(self.failed_icloud[:5]) + "). Ein erneuter Export versucht nur diese noch einmal.")
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
        self.tmpdir = make_workdir()
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
                "now": time.time(),
                "convert": {**converter_info(), "installing": _install_state["running"]},
            }

    def library(self) -> dict:
        out = {"albums": [], "people": [], "months": []}
        if not self.lib:
            return out
        bucket = {"album": "albums", "person": "people", "month": "months"}
        for gid, g in self.groups.items():
            assets = [self.lib.assets[pk] for pk in g.asset_pks]
            cover = max(assets, key=lambda a: a.created or 0).pk if assets else None
            item = {"id": gid, "title": g.title, "count": len(assets), "cover": cover,
                    "videos": sum(1 for a in assets if a.is_video)}
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
        media = body.get("media") or ("all" if body.get("videos", True) else "photos")
        opts.media = media if media in ("all", "photos", "videos") else "all"
        opts.release_icloud = bool(body.get("releaseIcloud", True))
        pl = body.get("placeLevel") or None
        opts.place_level = pl if pl in ("city", "state", "country") else None
        conv = body.get("convert") or None
        if conv:
            if conv not in {f["id"] for f in converter_info()["formats"]}:
                return converter_info()["error"] or f"Format {conv} wird nicht unterstützt."
            opts.convert = conv
            opts.quality = max(10, min(100, int(body.get("quality") or 90)))
            opts.keep_heic = bool(body.get("keepHeic", False))
        use_icloud = bool(body.get("icloud", True)) and self.icloud_dir is not None
        with self.lock:
            if self.phase != "ready":
                return "Gerade nicht möglich (keine Verbindung oder Export läuft schon)."
            self.phase = "exporting"
            self.export = {"running": True, "i": 0, "total": 0, "name": "", "target": str(target),
                           "stats": {"copied": 0, "icloud": 0, "skipped": 0, "missing": 0, "failed": 0,
                                     "converted": 0, "convFailed": 0, "bytes": 0},
                           "active": 0, "startedAt": time.time(),
                           "cancelled": False, "error": None, "missingExamples": [], "icloudUsed": use_icloud,
                           "waiting": None, "current": None, "convert": opts.convert}
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
            if u.path == "/api/install-converter":
                threading.Thread(target=install_converter, args=(app.log,), daemon=True).start()
                return self._send(200, {"ok": True})
            if u.path == "/api/icloud":
                ex = app.exporter
                if ex:
                    if body.get("action") == "skip":
                        ex.skip_icloud = True
                    elif body.get("action") == "restart":
                        ex.restart_icloud = True
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
    medien = p.add_mutually_exclusive_group()
    medien.add_argument("--ohne-videos", "--nur-fotos", dest="nur_fotos", action="store_true",
                        help="Nur Fotos exportieren, Videos weglassen")
    medien.add_argument("--nur-videos", action="store_true", help="Nur Videos exportieren, Fotos weglassen")
    p.add_argument("--ort", choices=["stadt", "bundesland", "land"],
                   help="In Alben und Monaten Unterordner nach Ort anlegen")
    p.add_argument("--format", choices=list(CONVERT_FORMATS), help="HEIC live in dieses Format umwandeln")
    p.add_argument("--qualitaet", type=int, default=90, help="Qualität 10–100 für JPG/WebP/AVIF/JPEG 2000")
    p.add_argument("--heic-behalten", action="store_true", help="Bei --format das HEIC-Original zusätzlich behalten")
    p.add_argument("--ohne-icloud", action="store_true",
                   help="Fehlende Originale NICHT aus dem iCloud-Fotos-Ordner (iCloud für Windows) holen")
    a = p.parse_args(argv)

    phone = IPhone()
    print(f"Verbunden: {phone.name} (iOS {phone.ios})")
    lib = load_library(phone, make_workdir())
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
    opts.media = "photos" if a.nur_fotos else "videos" if a.nur_videos else "all"
    opts.place_level = {"stadt": "city", "bundesland": "state", "land": "country"}.get(a.ort)
    if a.format:
        info = converter_info()
        if a.format not in {f["id"] for f in info["formats"]}:
            raise RuntimeError(info["error"] or f"Format {a.format} wird nicht unterstützt.")
        opts.convert, opts.quality, opts.keep_heic = a.format, max(10, min(100, a.qualitaet)), a.heic_behalten
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
