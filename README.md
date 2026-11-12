# iPhone Foto-Export (USB)

Exportiert Fotos und Videos vom iPhone in Ordner pro **Album** und/oder pro **Person**:

```
Zielordner\
  Alben\Urlaub 2025\IMG_1234.HEIC
  Alben\Urlaub 2025\IMG_1234.MOV        (Live-Photo-Video)
  Personen\Anna\IMG_1234.HEIC
```

## Starten
1. iPhone per USB anschließen, **entsperren**, „Diesem Computer vertrauen“ bestätigen.
2. Doppelklick auf **`iPhone Fotos starten.bat`**. Die Oberfläche öffnet sich im Browser.
3. „iPhone verbinden“ klicken, Alben/Personen antippen, unten Zielordner wählen, „Exportieren“.

Die Oberfläche läuft nur lokal auf deinem PC (127.0.0.1, mit Zugriffsschlüssel).
Schließt du den Browser-Tab, beendet sich das Tool nach kurzer Zeit von selbst
(ein laufender Export wird vorher noch fertig gemacht).

Dateien: `iphone_fotos.py` (Logik + lokaler Server), `ui.html` (Oberfläche).
Bei einem Startfehler steht der Grund in `iphone_fotos_fehler.log`.

## Gut zu wissen
- **Personen** erscheinen nur, wenn sie in der Fotos-App einen Namen haben.
- **iCloud-Fotos mit „iPhone-Speicher optimieren“**: Dann liegen viele Originale nur in iCloud,
  nicht auf dem iPhone. Solche Dateien werden als „nicht auf dem iPhone“ gemeldet.
  Abhilfe: Einstellungen → Fotos → „Laden und Originale behalten“ (braucht Speicherplatz).
- Ein Foto in mehreren Alben/Personen wird in jeden Ordner kopiert.
- „Vorhandene überspringen“ ist an, ein abgebrochener Export kann einfach neu gestartet werden.
- Dateidatum = Aufnahmedatum.
- Das Tool liest nur, es ändert nichts auf dem iPhone.

## Kommandozeile (optional)
```
python iphone_fotos.py --list
python iphone_fotos.py --album "Urlaub 2025" --person Anna --ziel D:\Fotos
python iphone_fotos.py --album ALLE --person ALLE
```

Voraussetzungen (bereits installiert): Python 3.12, `pymobiledevice3`, iTunes (Apple Mobile Device Service).
