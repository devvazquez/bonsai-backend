# Clips que les ulleres porten gravats

Àudio que sona a les ulleres **sense passar pel backend**, perquè ha de sonar
just quan no hi ha resposta encara.

| Fitxer | Què és | Mida |
| --- | --- | --- |
| `digam-16k.pcm` | «Diga’m!» — el que es copia a la SD | 16 KB · 0,52 s |
| `digam-16k.wav` | El mateix, amb capçalera, per escoltar-lo | 16 KB |

## Per a què serveix

Entre que es detecta «Hey Bonsai» i el micròfon s'obre hi ha un buit: s'ha de
fer la foto i pujar-la. En comptes de deixar-lo en silenci, les ulleres diuen
«Diga’m» mentre puja. Quan el clip acaba, s'obre el micròfon.

```
«Hey Bonsai!» -> foto -> comença la pujada
                         └─ sona «Diga’m» (0,52 s) ─┘
                                                    └─ s'obre el micròfon
```

Aquests dos torns són els que `/ask` li dona per dits al model
(`ASK_WAKE_PHRASE` i `ASK_WAKE_REPLY`), de manera que el que sent la persona i
el que llegeix el model són el mateix.

## El format

`pcm16` mono a 16.000 Hz, little-endian: **exactament el que vol l'I2S del
MAX98357A**. Es llegeix de la SD i s'escriu al bus tal qual, sense
descodificar res ni reservar memòria per a un WAV sencer.

El `.wav` és el mateix àudio amb 44 bytes de capçalera al davant. Només és per
escoltar-lo des de l'ordinador; a la SD hi va el `.pcm`.

## Com es regenera

Amb el mateix Piper i la mateixa veu que les respostes
(`ca_ES-upc_ona-medium`), perquè no es noti el salt entre el clip gravat i el
que contesta el model:

```bash
python generar_clips.py
```

O, si el que vols és baixar-lo directament al dispositiu al primer arrencada,
el backend ja el sap fer sense cap endpoint nou:

```bash
curl -X POST "$API/speak?text=Diga%E2%80%99m!&audioFormat=pcm16&sampleRate=16000" \
     -o digam-16k.pcm
```

Això últim té un avantatge: si algun dia canvies la veu al servidor, el clip
que es baixi ja serà la nova.

**Si canvies el text, canvia també `ASK_WAKE_REPLY` al `.env`.** Si no, el
model es pensarà que ha dit una cosa que la persona no ha sentit.

Piper posa una mica de soroll aleatori a cada síntesi, així que regenerar-lo
no dona un fitxer idèntic al byte. Sona igual; no t'espantis si el `git diff`
marca canvis.
