# iPhone Foto-Export (USB)

**Projektseite und Live-Demo:** https://jmdigitaltechnologies.github.io/Iphone_Bildexport_Tool/
· **Download:** [ZIP](https://github.com/JMDigitalTechnologies/Iphone_Bildexport_Tool/archive/refs/heads/main.zip)
· Kostenlos und Open Source ([MIT-Lizenz](LICENSE))

Exportiert Fotos und Videos vom iPhone in Ordner pro **Album**, pro **Person** und/oder nach **Jahr und Monat**:

```
Zielordner\
  Alben\Urlaub 2025\IMG_1234.HEIC
  Alben\Urlaub 2025\IMG_1234.MOV        (Live-Photo-Video)
  Personen\Anna\IMG_1234.HEIC
  Nach Datum\2025\07 Juli\IMG_1234.HEIC
```

Im Reiter **Datum** sind die Monate nach Jahren gruppiert; mit „Ganzes Jahr“ wählst du alle Monate eines Jahres auf einmal.
Grundlage ist das Aufnahmedatum; Fotos ohne Datum landen in `Nach Datum\Ohne Datum`.

## Starten
1. iPhone per USB anschließen, **entsperren**, „Diesem Computer vertrauen“ bestätigen.
2. Doppelklick auf **`iPhone Fotos starten.bat`**. Die Oberfläche öffnet sich im Browser.
3. „iPhone verbinden“ klicken, Alben/Personen/Monate antippen, unten Zielordner wählen, „Exportieren“.

Die Oberfläche läuft nur lokal auf deinem PC (127.0.0.1, mit Zugriffsschlüssel).
Schließt du den Browser-Tab, beendet sich das Tool nach kurzer Zeit von selbst
(ein laufender Export wird vorher noch fertig gemacht).

Dateien: `iphone_fotos.py` (Logik + lokaler Server), `ui.html` (Oberfläche).
Bei einem Startfehler steht der Grund in `iphone_fotos_fehler.log`.

## Gut zu wissen
- **Personen** erscheinen nur, wenn sie in der Fotos-App einen Namen haben.
- **iCloud-Fotos mit „iPhone-Speicher optimieren“**: Dann liegen viele Originale nur in iCloud,
  nicht auf dem iPhone, und lassen sich per USB nicht laden. Ist auf dem PC **iCloud für Windows**
  mit „Fotos“ eingerichtet, holt das Tool diese Originale automatisch aus dem Ordner
  `Bilder\iCloud Photos` (Windows lädt sie dabei aus iCloud). Die Sortierung kommt weiterhin vom iPhone.
  Läuft iCloud nicht oder fehlt die Anmeldung, startet das Tool iCloud, pausiert mit einem Hinweis
  und macht nach der Anmeldung automatisch weiter.
  Was auch dort fehlt (noch nicht synchronisiert), wird als „nicht gefunden“ gemeldet; ein späterer
  Export holt es nach. Alternative: auf dem iPhone Einstellungen → Fotos → „Laden und Originale behalten“.
- **HEIC live umwandeln** (Optionen → „HEIC live umwandeln“): Fotos werden während der Übertragung in
  JPG, PNG, WebP, AVIF, TIFF, JPEG 2000, BMP, GIF oder PDF umgewandelt, mit Qualitätsregler für
  JPG/WebP/AVIF/JPEG 2000. Ausrichtung wird übernommen, EXIF (Aufnahmedatum, GPS, Kamera) und Farbprofil
  bleiben bei JPG/PNG/WebP/AVIF erhalten (TIFF: Datum und Kamera, ohne GPS). Das HEIC-Original kann
  zusätzlich behalten werden. Die Umwandlung läuft parallel, die Übertragung wird dadurch kaum langsamer.
  Kommandozeile: `--format jpg --qualitaet 90 [--heic-behalten]`.
- **Schnell und parallel:** iPhone (3 gleichzeitige Übertragungen über USB) und iCloud (8 gleichzeitige
  Downloads) laufen parallel, die HEIC-Umwandlung auf bis zu 8 Prozessorkernen. Fotos kommen zuerst,
  große Videos zum Schluss. Das Export-Fenster zeigt Durchsatz, Dateien/s und Restzeit.
  Aus iCloud geladene Dateien werden danach wieder auf „nur online“ gesetzt (Option, Standard an),
  damit Laufwerk C: bei großen Exporten nicht vollläuft.
- **Unterordner nach Ort** (Optionen, bzw. `--ort stadt|bundesland|land`): Alben und „Nach Datum“ bekommen
  Unterordner nach Stadt, Bundesland/Region oder Land, z. B. `Nach Datum\2025\07 Juli\Rom (Italien)`.
  Der Ort stammt vom iPhone selbst (dieselben Namen wie in der Fotos-App, keine Internetabfrage);
  Orte im Ausland bekommen das Land dazu, Fotos ohne Ortsangabe landen in `Ohne Ort`.
- **Was exportieren?** In den Optionen wählbar: *Fotos & Videos* (Standard), *Nur Fotos* oder *Nur Videos*
  (Kommandozeile: `--nur-fotos` bzw. `--nur-videos`). Live-Photo-Clips gehören zu den Fotos und hängen
  an der eigenen Option „Live-Photo-Videos“.
- Ein Foto in mehreren Alben/Personen wird in jeden Ordner kopiert.
- „Vorhandene überspringen“ ist an, ein abgebrochener Export kann einfach neu gestartet werden.
- Dateidatum = Aufnahmedatum.
- Das Tool liest nur, es ändert nichts auf dem iPhone.

## Kommandozeile (optional)
```
python iphone_fotos.py --list
python iphone_fotos.py --album "Urlaub 2025" --person Anna --ziel D:\Fotos
python iphone_fotos.py --album ALLE --person ALLE
python iphone_fotos.py --datum 2025 --datum 2024-12      (ganzes Jahr / einzelner Monat)
python iphone_fotos.py --datum ALLE                      (komplette Mediathek nach Datum)
```

## Voraussetzungen
- Windows 10/11
- [Python 3.10+](https://www.python.org/downloads/windows/) (beim Installieren „Add python.exe to PATH“ anhaken)
- App „Apple Geräte“ oder iTunes (liefert den USB-Treiber fürs iPhone)
- Empfohlen bei „iPhone-Speicher optimieren“: iCloud für Windows mit aktivierten „Fotos“
- `pymobiledevice3` wird beim ersten Start automatisch installiert (`requirements.txt`)

## Projektseite (GitHub Pages)
Die Seite liegt in `site/`. Die Live-Demo ist `ui.html` im Demo-Modus: Wird die Datei nicht vom lokalen
Tool ausgeliefert, simuliert sie ein iPhone mit Beispieldaten. Der Workflow `.github/workflows/pages.yml`
baut beides bei jedem Push auf `main` und veröffentlicht es.

## Lizenz
MIT, siehe [LICENSE](LICENSE). Kein Produkt von Apple; iPhone ist eine Marke der Apple Inc.
