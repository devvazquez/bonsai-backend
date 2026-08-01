# Mesures i decisions tècniques

Aquest document recull **totes les mesures reals** que han decidit la
configuració actual del backend: proveïdor de visió, motor de veu, mida
d'imatge, format d'àudio. Res del que hi ha aquí és estimat — cada xifra ve
d'una prova real, repetible amb `bench_latency.py` o `test_bonsai.py`.

El README es queda amb el resum; aquí hi ha el perquè.

---

## Per què Python i no Cloudflare Workers

La primera versió d'aquest backend era a Cloudflare Workers. El requisit del
**català** obligava a fer servir edge-tts, que necessita un WebSocket amb
capçaleres pròpies sobre un socket TCP real: cosa que Workers no permet.

| Intent | Resultat |
| --- | --- |
| edge-tts a mà a Workers (fetch + Upgrade) | Microsoft torna centenars de frames binaris buits |
| npm `edge-tts-universal` a Workers | `ws` no hi funciona → WebSocket sense capçaleres → 403 |
| Workers AI (`melotts`) | Funciona, però no té català |
| Groq TTS | Només anglès i àrab |
| **edge-tts en Python** | ✅ Funciona, amb català |

Ara, amb el Piper en local, aquella dependència ja ni tan sols cal — però la
conclusió es manté: aquest servei necessita un procés Python de debò, no una
funció al límit.

## Latència: on se'n va el temps

Cada resposta porta els seus `timings` per poder repetir les mesures.

| Etapa | Temps | Comentari |
| --- | --- | --- |
| Reduir la foto al servidor | 192 ms | Només si arriba més gran de 896 px |
| Visió, Groq `qwen3.6-27b` | **552 ms** (551-554) | El més ràpid i el més regular |
| Visió, Gemini `3.1-flash-lite` | 844 ms (649-937) | Més irregular |
| Veu, Piper `upc_ona` | 205 ms | En local |
| Veu, edge-tts amb text nou | 1.320 ms (949-2.089) | Depèn de Microsoft |
| Escoltar l'àudio | ~10 s | 1-2 frases. És el tram més llarg de tots |

**La configuració més ràpida**, mesurada d'extrem a extrem:
`VISION_PROVIDER=groq`, imatge a 896 px i `pcm16` a 16 kHz → **primer byte
d'àudio a ~1.030 ms**.

El preu del Groq és la quota: 8.000 tokens/minut són unes **3 fotos per minut** a
896 px. Per desenvolupar sense barallar-se amb el 429, Gemini; per a la
latència, Groq.

### La trampa en mesurar edge-tts: Microsoft cacheja per text i veu

**Qualsevol mesura que repeteixi la mateixa frase és falsa**, i és fàcil caure-hi
sense adonar-se'n: n'hi ha prou d'haver sintetitzat aquell text en una prova
anterior, perquè la memòria cau és del servidor de Microsoft i sobreviu entre
execucions. Mesurat amb 8 frases catalanes noves, cadascuna un sol cop, i
després les mateixes 8 una altra vegada:

| | Primer tros | Àudio complet |
| --- | --- | --- |
| Text nou (el que passa en ús real) | mediana **1.092 ms**, fins a 1.864 | mediana **1.320 ms**, fins a 2.089 |
| Text repetit (memòria cau) | mediana 262 ms | mediana 430 ms |

Són **3,1 vegades** de diferència. A les ulleres el text sempre és nou, així que
la columna que compta és la primera. Les demos d'edge-tts que presumeixen de
0,4 s estan mesurant la memòria cau.

### Per què Piper, i quin maquinari necessita

[Piper](https://github.com/rhasspy/piper) té dues veus catalanes de la UPC
(`upc_ona`, `upc_pau`) i sintetitza **en local**, sense xarxa. Aquí no hi ha
memòria cau que enganyi, perquè cada síntesi es fa de zero:

| | Mediana | Rang | Notes |
| --- | --- | --- | --- |
| `upc_ona` medium, 22 kHz | **205 ms** | 182-422 | Model de 63 MB |
| `upc_pau` x_low, 16 kHz | **122 ms** | 105-219 | Model de 28 MB |
| edge-tts, text nou, 24 kHz | 1.320 ms | 949-2.089 | Depèn de Microsoft |

És **6,4 vegades més ràpid** que edge-tts amb text nou, i sobretot predictible:
sense xarxa, sense cua llarga i sense dependre d'un servei aliè. El model triga
~1,2 s a carregar-se un sol cop en arrencar.

Piper és **monofil a la pràctica**: 274 ms amb un sol nucli i 267 ms amb quatre.
Per a la latència **no serveix de res tenir més nuclis**, només compta la
potència d'un. Un VPS d'1 vCPU va igual de ràpid que un de 4.

**On anirà igual de ràpid**: qualsevol x86-64 amb AVX2 o AVX-512 i una
freqüència semblant, encara que tingui un sol vCPU.

**On anirà més lent**:

- **ARM (Raspberry Pi i similars)**: es reporten factors de ~5x temps real
  contra els 26x d'aquí, o sigui unes 4-5 vegades més lent (~1-1,5 s per frase).
  Allà compensa la veu `x_low`.
- **vCPU compartida o "burstable"** (AWS t3/t4g, plans "shared CPU"): va bé
  mentre quedin crèdits i després t'escanyen. La latència es torna
  impredictible, que és justament del que fugíem en deixar edge-tts.
- **CPU sense AVX2** (Intel anteriors a ~2013, alguns Atom i Celeron).

**On no funcionarà**: a la mateixa ESP32-S3. No és qüestió de velocitat, és que
no hi cap: el model són 63 MB (28 MB el `x_low`) contra 8 MB de PSRAM, i no hi
ha ONNX Runtime per a aquell microcontrolador.

**Concurrència**: com que cada síntesi ocupa un nucli, 4 nuclis donen unes **3
síntesis per segon** de rendiment total. Vuit peticions alhora es van resoldre
en 2.548 ms, però això és rendiment agregat, no latència: les últimes de la cua
esperen.

### La resta del TTS català: Matxa, alVoCat, StyleTTS2

El BSC (Barcelona Supercomputing Center) i el Projecte AINA han publicat força
TTS català, i és el millor que hi ha en qualitat. El problema per a les ulleres
és la velocitat.

Mesurat aquí mateix, amb la **mateixa locutora `ona`** que fa servir la nostra
veu del Piper i la mateixa frase, al mateix Xeon:

| Sistema | Mediana | Temps real | Model | Llicència |
| --- | --- | --- | --- | --- |
| **Piper `upc_ona` medium** | **205 ms** | 26x | 63 MB | MIT + veu CC BY-SA 3.0 ES |
| Matxa v2 central + WaveNeXt | 1.053 ms | 5,8x | 271 MB | Apache-2.0 |

Matxa és **5 vegades més lent** i el model pesa 4 vegades més. A canvi sona
millor: és un Matcha-TTS (flow matching) amb vocoder WaveNeXt, entrenat amb
festcat i openslr69, i porta 47 veus.

El que hi ha publicat, per si canvien les prioritats:

| Model | Què és | Llicència |
| --- | --- | --- |
| `BSC-LT/matxa-tts-v2-ca-central-graphemes` | El de la taula. 47 veus, 10 passos | **Apache-2.0** |
| `BSC-LT/matxa-tts-v2-ca-multiaccent-graphemes` | 16 veus, 4 dialectes, 20 passos | **No comercial** |
| `BSC-LT/styletts2-catalan-multispeaker` | StyleTTS2, més qualitat i més lent | GPL-3.0 |
| `projecte-aina/matxa-tts-cat-multiaccent` | Matxa v1 | GPL-3.0 |
| `projecte-aina/alvocat-vocos-22khz` | Vocoder de la v1 | CC |
| `BSC-LT/vocos-mel-22khz` | Vocoder Vocos | Apache-2.0 |

Dos avisos sobre llicències, que aquí importen més del normal:

- El **multiaccent** (balear, valencià, nord-occidental, central) és el més
  atractiu del catàleg, però la seva llicència és `custom-ro-nc-openrail-m`:
  *"free to use for non-commercial and research purposes. Commercial use is only
  possible through licensing by the voice artists"*. Si Bonsai arriba a ser un
  producte, cal parlar amb el BSC i amb La Fresca Produccions.
- Les **v1 són GPL-3.0**, que és copyleft. La v2 central és Apache-2.0 i és
  l'única de les bones sense lligams.

**Conclusió: es manté el Piper.** 205 ms contra 1.053 ms és la diferència entre
unes ulleres que responen i unes que es fan esperar. Si algun dia la veu importa
més que la latència, Matxa v2 central és l'elecció.

### Descartat: el TTS de Gemini

Suporta català, però mesurat amb la mateixa frase és **inservible per a això**:

| Model | Latència |
| --- | --- |
| `gemini-2.5-flash-preview-tts` | 5.354 ms i 7.727 ms |
| `gemini-3.1-flash-tts-preview` | 11.320 ms |

Entre 4 i 55 vegades més lent que el Piper, i a sobre gastant de la mateixa
quota que la visió.

### Reduir la imatge: depèn del proveïdor

**Groq cobra per píxels**, així que reduir és la diferència entre poder provar i
no poder:

| Imatge | Mida | Latència de visió | Tokens d'entrada |
| --- | --- | --- | --- |
| 3024x4032 (foto de mòbil) | 3,1 MB | 2,4-3,8 s | ~50.000 |
| 672x896 (costat llarg 896 px) | 64 KB | **1,07-1,16 s** | **2.656** |

Els 2.656 no són cap estimació: els va dir el mateix error 429 de Groq
(`Requested 2656`). Amb la foto sense reduir es van esgotar els 200.000 tokens
del dia en unes sis peticions.

**Gemini cobra pla.** Mesurat amb el seu endpoint `countTokens`, que és gratis:
la mateixa foto costa **1.108 tokens tant a 256x170 com a 2400x1597**. Reduir-la
no estalvia ni un token. El que canvia el preu és `mediaResolution`:

| `mediaResolution` | Tokens | Resultat amb la foto de prova |
| --- | --- | --- |
| `LOW` | 286 | Va confondre la plaça |
| `MEDIUM` | 577 | Mateixa resposta que `HIGH` |
| sense especificar (= `HIGH`) | 1.133 | Correcta |

**Però amb Gemini també compensa reduir, encara que no estalviï tokens**: amb la
mateixa foto de 3024x4032, la visió va passar de **2.342 ms sense reduir a
988 ms a 896 px**. El que es paga amb la foto gran no són tokens, és pujar-la i
processar-la.

Per això **el servidor la redueix ell mateix** (`imagen.py`), per als dos
proveïdors. Costa poc:

| Costat llarg | CPU | 977 KB es queden en |
| --- | --- | --- |
| 672 px | 168 ms | 60 KB |
| **896 px** (per defecte) | **192 ms** | **92 KB** |
| 1120 px | 273 ms | 127 KB |

Respecta l'orientació EXIF, així que les fotos de mòbil deixen de descriure's
tombades, i si alguna cosa falla envia l'original en comptes de tirar la
petició avall.

## Coses que ja s'han provat i decidit

- **Res de raonament pas a pas, als dos proveïdors.** `reasoning_effort: "none"`
  al Groq no és decoratiu: sense això el model escriu un bloc `<think>` que es
  menja els 150 tokens i torna la resposta truncada.

  A Gemini passa exactament el mateix i per això va
  `thinkingConfig: {thinkingLevel: "minimal"}`. Amb `"medium"`: gasta ~140
  tokens pensant, esgota els 150 de `maxOutputTokens` i torna
  `finishReason: MAX_TOKENS` amb la frase tallada a mitja paraula. Compte amb el
  lloc exacte del camp: `thinkingLevel` solt a `generationConfig` el rebutja amb
  un 400.
- **Una sola connexió amb el proveïdor** per a tot el procés: el handshake TLS
  són ~220 ms mesurats que ara es paguen un cop. A més s'obre en arrencar
  (`vision.warmup()`), i el escalfament demana un llistat de models: no gasta ni
  un token.
- **Respostes d'1 o 2 frases** (prompt + `max_completion_tokens: 150`): escurça
  les dues etapes que escalen amb la longitud. Mesurat amb la mateixa foto,
  passar de 49 a 23 paraules baixa la locució de ~21 s a ~10 s.
- **No inventar-se topònims.** Amb una foto d'una plaça de Reus, el model deia
  "plaça de l'Ajuntament de Vilanova i la Geltrú" i, amb una altra resolució,
  "Plaza Real de Barcelona". Cap de les dues correcta, i dites amb tot l'aplom.
  Qui porta les ulleres no té manera de saber que és mentida, així que el prompt
  ara prohibeix endevinar noms propis: només els diu si els llegeix en un
  rètol.
- **Descartat amb dades: el streaming de la visió cap al TTS.** El primer token
  arriba a 1.246 ms i la frase sencera a 1.303 ms: 57 ms de diferència. El model
  escriu la frase curta de cop, així que enviar-la al TTS a trossos no estalvia
  res apreciable.

## Mesurar-ho tu mateix: `bench_latency.py`

Mesura el desglossament (reduir, codificar, xarxa, visió, TTS) i **per defecte
no gasta quota**: ensenya quantes peticions faria i quants tokens costaria, i no
crida ningú fins que li ho confirmes amb `--yes`.

```sh
# 0 tokens: comprova el codi (format d'imatge, 429, payloads, errors)
python bench_latency.py --selftest

# 0 tokens: assaig, diu què costaria
python bench_latency.py --provider both --image foto.jpg

# Gasta quota: el mínim per tenir la dada
python bench_latency.py --provider groq --image foto.jpg --only-small --yes

# Compara proveïdors amb la mateixa foto
python bench_latency.py --provider both --image foto.jpg --sizes 896 --yes

# Time to first token
python bench_latency.py --provider gemini --image foto.jpg --mode ttft --yes
```

S'atura tot just veure un 429, per no insistir contra una quota esgotada, i
avorta abans de començar si el pla passa de `--budget` (20.000 tokens per
defecte).

## Configurar la càmera de l'ESP32 (OV3660)

La XIAO ESP32S3 Sense munta un **OV3660: 3 MP, 2048x1536 (QXGA), 1/5"**, a les
revisions noves (les velles portaven un OV2640 de 2 MP).

Capturar a la resolució màxima és llençar el temps i la quota. El que interessa
és que la foto **ja surti de la càmera amb la mida bona**, perquè si el costat
llarg no passa de `IMAGE_MAX_SIDE` (896 per defecte) el servidor no la toca i
s'estalvia el remostreig:

| `frame_size` | Píxels | KB | Tokens a Groq | El servidor la redueix? |
| --- | --- | --- | --- | --- |
| `FRAMESIZE_VGA` | 640x480 | 58 | 1.353 | No |
| **`FRAMESIZE_SVGA`** | **800x600** | **81** | **2.115** | **No** |
| `FRAMESIZE_XGA` | 1024x768 | 114 | 3.464 | Sí (+~200 ms) |
| `FRAMESIZE_UXGA` | 1600x1200 | 208 | 8.458 | Sí |
| `FRAMESIZE_QXGA` | 2048x1536 | 290 | 13.858 | Sí |

**SVGA és el punt bo**: és 4:3 com el sensor (no retalla), no dispara el
remostreig al servidor i continua llegint rètols. Comprovat d'extrem a extrem:
`redueix 0 ms`, visió 883 ms. Amb QXGA gastaries 6,5 vegades més quota perquè el
servidor ho tiri tot a 896 px igualment.

```c
camera_config_t config = {
    // ... els pins de la XIAO ...
    .xclk_freq_hz = 20000000,
    .pixel_format = PIXFORMAT_JPEG,     // que comprimeixi el sensor, no la CPU
    .frame_size   = FRAMESIZE_SVGA,     // 800x600
    .jpeg_quality = 12,                 // compte: 0-63 i MENYS és MILLOR
    .fb_count     = 2,                  // l'OV3660 en necessita 2
    .fb_location  = CAMERA_FB_IN_PSRAM,
    .grab_mode    = CAMERA_GRAB_LATEST, // si no, t'emportes el fotograma vell
};

// Ajustos propis de l'OV3660, de l'exemple oficial CameraWebServer
sensor_t *s = esp_camera_sensor_get();
if (s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);         // l'OV3660 ve del revés
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);   // satura de més
}
```

Quatre coses que solen donar guerra:

- **`jpeg_quality` va al revés** que a tot arreu: 0-63 i com **menys**, millor
  qualitat i més bytes. 10-12 va bé; amb 30 el model comença a fallar en llegir
  text.
- **Llença els 2 o 3 primers fotogrames** després d'encendre la càmera:
  l'autoexposició i el balanç de blancs triguen a assentar-se i les primeres
  fotos surten fosques o verdoses.
- **PSRAM**: la XIAO porta OCTAL, no QUAD (`CONFIG_SPIRAM_MODE_OCT=y`). Amb
  l'OV3660 cal a més `CONFIG_CAMERA_PSRAM_DMA_MODE=n`, i baixar
  `CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL` a 4096 perquè quedi memòria per al WiFi.
- **No capturis en RGB565 per comprimir després per programari.** Amb
  `PIXFORMAT_JPEG` comprimeix el mateix sensor i surt de franc.

I una cosa del firmware que val més que qualsevol optimització del servidor:
**mantén la connexió TLS oberta** entre fotos. El handshake és el més car de tot
el costat del dispositiu.

## Altres apunts

- **Model de Groq**: Groq reanomena i retira models sovint. Si `/look`
  comença a donar error 502, comprova el nom vigent a
  <https://console.groq.com/docs/models> i canvia'l amb `GROQ_VISION_MODEL`,
  sense tocar el codi.
- **Límits del pla gratuït de Groq**: a 896 px cada foto gasta **2.656 tokens**,
  i el pla dóna 8.000 tokens per minut i 200.000 al dia. Surten unes **3 fotos
  per minut i ~75 al dia**. El límit que molesta en el dia a dia és el del
  minut, no el del dia.
- **edge-tts** fa servir un protocol que Microsoft no documenta oficialment.
  Funciona bé, però convé saber que es podria trencar si Microsoft el canviés.
  Amb el Piper per defecte, això ha deixat de ser un risc del camí principal.
- **Memòria**: de moment només desa el que se li envia explícitament a
  `POST /memory` o des del panell `/admin`. El següent pas natural seria
  demanar al model que tornés un camp `{"remember": "..."}` i desar-ho sol.
- **Límit de records**: 50 per dispositiu (`MAX_ITEMS` a `memory.py`), perquè el
  prompt no creixi sense control.
