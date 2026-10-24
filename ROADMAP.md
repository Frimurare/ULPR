# ULPR – plan och roadmap

Målet: en liten tjänst som står bredvid videoinspelaren hos kunden, läser registreringsskyltar
från vanliga övervakningskameror på en lokal GPU och levererar dem till OpenEye Web Services
som analyshändelser, så att skyltar blir sökbara i videosöket och kan driva larmregler.
Ingen molnanalys, inga licenser per kamera. Mjukvaran är fri (AGPL-3.0); det som säljs är
kameror, hårdvara och installation.

## Arkitektur

RTSP från kameran → skyltdetektor (YOLO, GPU) → OCR (fast-plate-ocr, ONNX, CPU) →
röstning över flera bildrutor → formatkontroll (svenska skyltar) → avduplicering →
loggning, databas och bildutsnitt → OpenEye Event API (`ANALYTIC_LICENSE_PLATE_DETECTED`).

En delad detektor betjänar flera kameror. Konfiguration och drift sker i ett webbgränssnitt
på svenska. Hemligheter (kamerainloggning, OpenEye-nycklar) lagras lokalt med begränsade
rättigheter och visas aldrig i klartext.

## Versioner

### 0.1 Prototyp – klar 2026-10-24
- Hela kedjan på en Quadro RTX 5000 mot en 5 Mpix Axis-kamera: 24 ms per bildruta,
  cirka 9 bildrutor/s, under 500 MB grafikminne, cirka 1 s från bildruta till händelse.
- OCR vald efter test av fem modeller: fast-plate-ocr (34 av 36 rätt, 3 ms per skylt).
- Svenska formatregler, röstning, avduplicering, lagringstid 30 dagar.
- Verifierad leverans till OpenEye (202) och läsningar ned till 55 px skyltbredd.

### 0.2 Business ready – pågår
- Webbgränssnitt på svenska med inloggning: översikt med status och senaste läsningar,
  kameror (flera, lägg till/ändra/testa), OpenEye-nycklar med testhändelse, utseende med
  uppladdning av egen logotyp (standard Prudencia), historik med sökning och CSV-export,
  systemsida med logg, lagringstid och omstart.
- Konfiguration i databas, en systemd-tjänst, loggrotation, daglig städning.
- Hjälpare som hämtar kameralistan från en OpenEye-inspelare och fyller i kamera-id.

### 0.3 Fält och kvalitet
- Begränsningsområde (ROI) per kamera för att bara läsa infarten, inte gatan.
- Nattläge och IR: exponering, reflexer, trösklar per kamera.
- Insamling av bildutsnitt per installation som underlag för finjustering av OCR på
  svenska skyltar (snö, smuts, nya skyltformat).
- Hälsorapportering (heartbeat, kamerabortfall, GPU-fel) till Prudencias övervakning.
- Händelser även som scenario-text för tydliga larm i OpenEye.

### 0.4 Liten hårdvara
- Port till Raspberry Pi 5 med AI HAT+ 2 (Hailo-10H) för 2–4 kameror per enhet:
  modeller kompilerade till HEF, H.265-huvudström, samma webbgränssnitt.
- Alternativ GPU-klass för kontorsdatorer (RTX A2000 / RTX 2000 Ada).
- Tvåstegslarm: Pi-enheten ger första triggern (person, fordon, rök) som analyshändelse till
  OpenEye, och OpenEyes AI Visual Check gör den andra kontrollen med ett fritextvillkor innan
  det blir ett skarpt larm. Billigt första filter, få falsklarm, Visual Check bara vid behov.

### 1.0 Produkt
- Installationspaket (Ubuntu) och uppdateringsmekanism.
- Säkerhetsgenomgång, backup och återställning av konfiguration.
- Flerspråkigt gränssnitt (svenska standard).
- Egen tränad detektor under fri licens om licensläget kräver det.

## Licensläge
- Ultralytics YOLO och de skyltvikter som använts under utvecklingen är AGPL-3.0.
- fast-plate-ocr är MIT.
- Projektet publiceras därför i sin helhet under AGPL-3.0.

## Integritet
Registreringsnummer är personuppgifter. Tjänsten har kort lagringstid, begränsat synfält
(ROI) och lokal lagring. Ändamål och skyltning avgörs per installation.
