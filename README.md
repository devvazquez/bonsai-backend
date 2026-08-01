# Bonsai Backend

Backend de **Bonsai**, unes ulleres intel·ligents que expliquen en veu alta què
tens al davant: per orientar-te, per llegir un cartell o un menú, per saber què
és un objecte o simplement per curiositat.

```mermaid
flowchart LR
    A["Ulleres<br/>(ESP32-S3 + OV3660)"] -- "POST /look<br/>(foto en base64)" --> C["Aquest backend"]
    C -- redueix a 896 px --> D["Gemini o Groq"]
    D -- descripció --> C
    C -- text --> E["Piper<br/>(en local, veu catalana)"]
    E -- mostres --> C
    C -- "PCM16 en streaming" --> A
    A -- I2S --> F["MAX98357A"]
```

**L'ESP32 crida l'API directament**, sense cap aplicació web al mig. Per això
hi ha un únic endpoint d'imatge, `/look`, fet a mida del microcontrolador: rep
la foto i torna l'àudio en cru i en streaming, llest per a l'I2S.

- **Visió**: **Groq** per defecte (552 ms, molt regular, quota justa) o
  **Gemini** (més lent, quota molt més folgada per desenvolupar).
- **Veu**: **Piper** en local per defecte (205 ms, veu catalana
  `ca_ES-upc_ona-medium`, sense xarxa). edge-tts disponible com a alternativa.
- **Memòria**: SQLite, un fitxer, sense serveis externs.

Amb la configuració per defecte, el **primer byte d'àudio surt a ~1,0-1,8 s**
des que arriba la foto. Totes les xifres d'aquest README estan mesurades, no
estimades — el detall i el perquè de cada decisió és a
**[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**.

---

## Tres pàgines, per provar-ho i per administrar-ho

El mateix backend les serveix totes tres, sense dependències ni CORS.

<table>
<tr><td width="50%">

**[`/provar`](static/probar.html)** — fes una foto des del mòbil i escolta la
resposta. Redueix la imatge abans de pujar-la i ensenya el desglossament de
temps.

<img src="docs/img/provar.png" width="320" alt="Pàgina /provar en un mòbil, mostrant una foto d'una plaça, el text descrit i la taula de temps">

</td><td width="50%">

**[`/memoria`](static/memoria.html)** — veure, afegir, editar i esborrar els
records de cada dispositiu sense tocar SQL.

<img src="docs/img/memoria.png" width="480" alt="Pàgina /memoria mostrant la llista de dispositius i els records d'un d'ells">

</td></tr>
</table>

Per a tot el que `/memoria` no cobreix hi ha **`/admin`**: explorar qualsevol
taula, crear-ne, afegir columnes, editar files i executar SQL a mà.

<img src="docs/img/panel.png" width="640" alt="Panell /admin amb la llista de taules a l'esquerra i les files de memories a la dreta">

<img src="docs/img/panel-estructura.png" width="640" alt="Pestanya Estructura del panell, amb el CREATE TABLE, les columnes i els índexs">

Això és **accés SQL complet a la base de dades**, així que la ruta només
existeix si defineixes `ADMIN_PASSWORD`:

```bash
openssl rand -hex 16        # i posa-la a .env com a ADMIN_PASSWORD
```

Sense la variable, `/admin` no es munta i torna 404. Amb ella, demana la
contrasenya abans de deixar veure res.

---

## Provar-ho en local

Necessites **Python 3.11+** i una clau gratuïta de
[console.groq.com](https://console.groq.com) (o de
[aistudio.google.com/apikey](https://aistudio.google.com/apikey) per Gemini).
La veu no necessita cap clau: Piper és local i es baixa sol (63 MB) la primera
vegada.

```bash
git clone https://github.com/devvazquez/bonsai-backend.git
cd bonsai-backend
./run-local.sh          # Windows: .\run-local.ps1
```

El primer cop crea el `.env` i s'atura perquè hi posis la `GROQ_API_KEY`. El
tornes a executar i arrenca amb recàrrega automàtica.

- API: <http://127.0.0.1:8080> · Swagger: `/docs`
- Provar amb una foto: `/provar` · Administrar la memòria: `/memoria`
- Administrar la base de dades: `/admin` (només amb `ADMIN_PASSWORD` definida)
- `/health` diu quin proveïdor hi ha actiu i si Piper ha carregat bé

<details>
<summary>Arrencar-ho a mà, sense l'script</summary>

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env          # i posa-hi la teva GROQ_API_KEY
uvicorn main:app --host 127.0.0.1 --port 8080 --reload
```
</details>

<details>
<summary>Terminal de proves (test_bonsai.py), sense dependències</summary>

```bash
python test_bonsai.py
```

```
bonsai> describe image.png              # imatge -> text -> àudio, amb temps
bonsai> speak Hola, les ulleres funcionen
bonsai> remember Al Biel li agrada el cafè sense sucre
bonsai> config provider gemini          # canviar de proveïdor sense reiniciar
bonsai> help
```
</details>

---

## API

| Mètode | Ruta | Descripció |
| --- | --- | --- |
| `POST` | `/look` | **Endpoint principal**: foto → àudio en cru i en streaming |
| `POST` | `/speak?text=...&lang=ca` | Només text a veu |
| `GET`/`POST` | `/memory` | Dispositius i records (veure `/memoria`) |
| `PATCH`/`DELETE` | `/memory/{deviceId}/{id}` | Corregeix o esborra un record |
| `GET` | `/voices?prefix=ca` | Veus disponibles |
| `GET` | `/health` | Estat del servei. Sense autenticació |
| `GET` | `/admin` | Panell de la base de dades. Amb `ADMIN_PASSWORD` |

### `POST /look`

```jsonc
{
  "image": "<base64 sense el prefix data:image/...>",
  "deviceId": "bonsai-01",
  "prompt": "Què diu el cartell?",     // opcional; per defecte, descripció general
  "lang": "ca",                        // ca | es | en
  "provider": "groq",                  // groq | gemini. Per defecte, VISION_PROVIDER
  "tts": "piper",                      // piper | edge. Per defecte, TTS_PROVIDER
  "audioFormat": "pcm16",              // pcm16 | mulaw | wav, o mp3 amb edge
  "sampleRate": 16000                  // 8000 | 16000 | 22050
}
```

**El cos de la resposta és l'àudio**, en cru i en streaming. Tot el context va
a capçaleres (el text en base64, perquè les capçaleres HTTP són ASCII):

```
X-Bonsai-Text: <base64>        X-Bonsai-Provider: groq
X-Bonsai-Format: pcm16         X-Bonsai-Rate: 16000
X-Bonsai-Vision-Ms: 552        X-Bonsai-Resize-Ms: 192
```

Des d'un navegador, `"audioFormat": "wav"` (es posa directe a un `<audio>`).
Des de l'ESP32, `pcm16` a 16 kHz: enters de 16 bits amb signe, little-endian,
mono — exactament el que vol l'I2S del MAX98357A, sense conversió. Amb
`"tts": "edge"` el format només pot ser `mp3`.

| Codi | Vol dir |
| --- | --- |
| `429` | Quota esgotada. Ve amb `Retry-After` en segons: reintenta, no és una errada |
| `502` | El proveïdor ha fallat de debò; el detall va al cos |
| `500` | Falta la clau del proveïdor triat |
| `400` | Paràmetres invàlids (`provider`, `tts`, `audioFormat`...) |

Tots els endpoints excepte `/health`, `/provar` i `/memoria` demanen
`X-API-Token` si `BONSAI_API_TOKEN` està definit. `/admin` no fa servir el
token: té la seva pròpia contrasenya (`ADMIN_PASSWORD`).

---

## Desplegar-ho en un VPS

```bash
git clone https://github.com/devvazquez/bonsai-backend.git && cd bonsai-backend
cp .env.example .env
openssl rand -hex 32        # token per BONSAI_API_TOKEN
nano .env                   # GROQ_API_KEY, BONSAI_API_TOKEN, BONSAI_DOMAIN
```

**Si el VPS no té cap proxy**, Caddy gestiona l'HTTPS sol (Let's Encrypt
inclòs; el DNS ha d'apuntar ja al VPS):

```bash
docker compose --profile caddy up -d --build
```

**Si ja hi ha Nginx o un altre proxy**, aixeca només l'app (`127.0.0.1:8080`)
i afegeix-hi això:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_read_timeout 60s;
    proxy_buffering off;     # imprescindible: /look va en streaming

    # El panell /admin parla per WebSocket (/socket.io). Sense aquestes dues
    # línies carrega la pàgina i es queda en blanc.
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Amb Caddy no cal fer res: ja passa els WebSockets sol.

Comprova amb `curl http://localhost:8080/health` — mira que `keyConfigured`
sigui `true` pel teu proveïdor i que `tts.active` coincideixi amb
`tts.configured` (si no, Piper ha fallat i el motiu és a `tts.piper.error`).

```bash
git pull && docker compose up -d --build   # actualitzar
docker compose logs -f bonsai              # logs
docker compose cp bonsai:/data/bonsai.db ./backup-$(date +%F).db
```

**Recursos**: 1 GB de RAM (Piper en fa servir ~210 MB, el panell ~40 MB) i n'hi ha prou amb
**1 vCPU** — Piper és monofil, més nuclis no baixen la latència.

---

## Variables d'entorn

| Variable | Per defecte | Per a què serveix |
| --- | --- | --- |
| `VISION_PROVIDER` | `groq` | `groq` o `gemini` |
| `GROQ_API_KEY` | — | **Obligatòria** amb el proveïdor per defecte |
| `GEMINI_API_KEY` | — | Obligatòria només si fas servir `gemini` |
| `TTS_PROVIDER` | `piper` | `piper` (local) o `edge` (Microsoft) |
| `BONSAI_API_TOKEN` | buit | Protegeix l'API. Buit = sense autenticació |
| `IMAGE_MAX_SIDE` | `896` | Costat llarg al qual es redueix la foto. `0` ho desactiva |
| `ADMIN_PASSWORD` | buit | Contrasenya de `/admin`. Buida = la ruta ni existeix |
| `BONSAI_DOMAIN` | — | Domini per a l'HTTPS (perfil `caddy`) |
| `ALLOWED_ORIGINS` | `*` | Orígens CORS (només rellevant si hi ha una app web) |

Més variables (models concrets, `GEMINI_THINKING_LEVEL`, `PIPER_VOICES_DIR`...)
a [`.env.example`](.env.example), amb el perquè de cada valor per defecte.

---

## Estructura

| Fitxer | Contingut |
| --- | --- |
| `main.py` | L'API: endpoints, autenticació, construcció del prompt |
| `vision.py`, `groq_vision.py`, `gemini_vision.py` | Proveïdors de visió |
| `imagen.py` | Redueix la foto abans d'enviar-la al proveïdor |
| `tts.py`, `piper_tts.py` | Motors de veu (Piper en local, edge-tts) |
| `memory.py` | Records per dispositiu en SQLite |
| `static/probar.html`, `static/memoria.html` | Les pàgines `/provar` i `/memoria` |
| `panel.py` | El panell `/admin` per administrar la base de dades |
| `test_bonsai.py` | Terminal de proves, sense dependències |
| `bench_latency.py` | Banc de proves de latència, amb topall de quota |
| `docs/BENCHMARKS.md` | Totes les mesures i el perquè de cada decisió |

Per a l'ESP32 (configuració de la càmera OV3660, formats d'àudio en detall) i
per a qualsevol xifra de latència, quota o comparativa: **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**.
