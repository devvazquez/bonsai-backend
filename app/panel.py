"""Panel d'administració de la base de dades, muntat dins del mateix FastAPI.

Substitueix DbGate. Fa el mateix que necessitem d'ell —explorar taules, editar
files, crear taules i columnes, executar SQL— però integrat al backend: un sol
contenidor, un sol port, un sol login, i amb l'aspecte del projecte en comptes
d'una interfície genèrica.

Va amb NiceGUI, que serveix Vue i Quasar des del mateix paquet: no depèn de cap
CDN, així que funciona igual en una VPS sense sortida a internet.

La paleta ve de la skill ui-ux-pro-max (perfil "Space Tech"): fons gairebé
negre i blau com a únic accent.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from typing import Any

from nicegui import app as nicegui_app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from . import memory

# Contrasenya per entrar-hi. Això és accés SQL complet a la base de dades, així
# que si no n'hi ha cap de definida el panell no es munta: val més que no hi
# sigui que no pas que hi sigui obert.
CONTRASENYA = os.environ.get("ADMIN_PASSWORD", "")

# Prefix on està muntat el panell. El fixa construeix(). Només cal per al
# middleware, que veu el camí sencer; dins de les pàgines, ui.navigate.to()
# ja hi posa el prefix sol.
PREFIX = "/admin"

# --------------------------------------------------------------------------
# Paleta
# --------------------------------------------------------------------------
C = {
    "bg": "#0B0B10",        # fons, gairebé negre
    "surface": "#121218",   # panells
    "card": "#1A1A21",      # elevat
    "line": "#242430",      # vores
    "line2": "#33333F",     # vores destacades
    "fg": "#F8FAFC",
    "dim": "#A1A1B5",
    "faint": "#6C6C82",
    "accent": "#3B82F6",    # blau
    "accent2": "#60A5FA",
    "danger": "#EF4444",
    "ok": "#34D399",
    "warn": "#FBBF24",
}

# Files per pàgina en explorar una taula. Suficient per treballar sense
# convertir la pàgina en una descàrrega de tota la base de dades.
PER_PAGINA = 50

CSS = f"""
:root {{
  --bg:{C['bg']}; --surface:{C['surface']}; --card:{C['card']};
  --line:{C['line']}; --line2:{C['line2']}; --fg:{C['fg']};
  --dim:{C['dim']}; --faint:{C['faint']}; --accent:{C['accent']};
  --danger:{C['danger']};
}}
body, .nicegui-content {{ background:var(--bg); color:var(--fg); }}
.q-page, .nicegui-content {{ padding:0 !important; }}

/* Tipografia: pila del sistema per no dependre de Google Fonts, i
   monoespaiada de debò per a les dades, que és on cal alinear. */
body {{ font-family: ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
        -webkit-font-smoothing:antialiased; }}
.mono {{ font-family: ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace; }}

.panel {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; }}
.hairline {{ border-color:var(--line) !important; }}

/* Barra lateral */
.taula-item {{ border-radius:8px; cursor:pointer; transition:background .15s,color .15s; }}
.taula-item:hover {{ background:var(--card); }}
.taula-item[data-sel="true"] {{ background:rgba(59,130,246,.14);
  box-shadow:inset 2px 0 0 var(--accent); }}

/* Taula de dades */
.q-table__container {{ background:transparent !important; }}
.q-table thead th {{
  background:var(--surface) !important; color:var(--faint) !important;
  font-size:11px !important; font-weight:600 !important; letter-spacing:.06em;
  text-transform:uppercase; border-bottom:1px solid var(--line) !important;
  position:sticky; top:0; z-index:2;
}}
.q-table tbody td {{
  color:var(--fg) !important; border-bottom:1px solid var(--line) !important;
  font-size:13px !important;
  font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}
.q-table tbody tr {{ transition:background .12s; }}
.q-table tbody tr:hover {{ background:var(--card) !important; }}
.q-table__bottom {{ border-top:1px solid var(--line) !important; color:var(--dim) !important; }}

/* Controls */
.q-field__control {{ background:var(--bg) !important; border-radius:8px !important; }}
.q-field__native, .q-field__input, textarea {{ color:var(--fg) !important; }}
.q-field--outlined .q-field__control:before {{ border-color:var(--line2) !important; }}
.q-field--outlined.q-field--focused .q-field__control:after {{
  border-color:var(--accent) !important; border-width:1px !important; }}
.q-btn {{ text-transform:none !important; font-weight:500; letter-spacing:0; }}
.q-btn:not(.q-btn--flat) {{ border-radius:8px; }}

/* Accessibilitat: focus visible sempre, i respectar qui demana no-animacions */
*:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
@media (prefers-reduced-motion:reduce) {{
  * {{ animation:none !important; transition:none !important; }}
}}

/* Els diàlegs són q-card, que porta la seva pròpia vora clara */
.q-dialog .q-card {{ border:1px solid var(--line2) !important;
  background:var(--surface) !important; }}

/* Les dades poden ser més amples que la pantalla: que faci scroll la taula i
   no la pàgina sencera. */
.scroll-x {{ width:100%; overflow-x:auto; }}

/* Mòbil: la barra lateral passa a dalt en comptes de robar amplada */
@media (max-width:860px) {{
  /* NiceGUI posa flex-wrap:nowrap a .nicegui-row, cal guanyar-li */
  .cos {{ flex-wrap:wrap !important; }}
  .lateral {{ width:100% !important; border-right:none !important;
    border-bottom:1px solid var(--line);
    /* que ocupi el que necessita, no tota l'alçada de la pantalla */
    align-self:flex-start; }}
  /* flex:1 amb wrap es queda a amplada zero: cal dir-li que ocupi la fila */
  /* l'estil en línia posa flex:1 (basis 0), que aquí deixa la columna
     a zero d'amplada: cal donar-li la fila sencera */
  .principal {{ flex:1 1 100% !important; }}
}}

.badge {{ font-size:11px; padding:1px 7px; border-radius:20px;
  border:1px solid var(--line2); color:var(--dim); font-variant-numeric:tabular-nums; }}
.chip-tipus {{ font-size:10px; letter-spacing:.05em; color:var(--faint);
  text-transform:uppercase; }}
"""


# --------------------------------------------------------------------------
# Accés a la base de dades
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(memory.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _cita(nom: str) -> str:
    """Cita un identificador per a SQL.

    Els noms de taula i columna no es poden passar com a paràmetre, així que
    van dins de la consulta. Doblar les cometes és el que impedeix que un nom
    com `a"; DROP TABLE x --` es converteixi en una altra instrucció.
    """
    return '"' + str(nom).replace('"', '""') + '"'


def taules() -> list[dict[str, Any]]:
    with _conn() as c:
        noms = [
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = []
        for n in noms:
            try:
                total = c.execute(f"SELECT COUNT(*) FROM {_cita(n)}").fetchone()[0]
            except sqlite3.Error:
                total = 0
            out.append({"nom": n, "files": total})
    return out


def columnes(taula: str) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(f"PRAGMA table_info({_cita(taula)})")]


def indexs(taula: str) -> list[dict[str, Any]]:
    with _conn() as c:
        out = []
        for r in c.execute(f"PRAGMA index_list({_cita(taula)})"):
            cols = [
                x["name"]
                for x in c.execute(f"PRAGMA index_info({_cita(r['name'])})")
            ]
            out.append({"nom": r["name"], "unic": bool(r["unique"]), "cols": cols})
    return out


def ddl(taula: str) -> str:
    with _conn() as c:
        r = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (taula,)
        ).fetchone()
    brut = (r["sql"] if r else "") or ""
    # El CREATE TABLE es guarda tal com es va escriure, sagnat inclòs. Aquí
    # queda desalineat, així que es reindenta a dos espais.
    linies = brut.splitlines()
    return "\n".join(
        [linies[0].strip()] + ["  " + l.strip() for l in linies[1:] if l.strip()]
    ) if linies else ""


def files(taula: str, pagina: int, cerca: str = "") -> tuple[list[dict], int]:
    cols = [c["name"] for c in columnes(taula)]
    where, params = "", []
    if cerca.strip() and cols:
        # Cerca en totes les columnes alhora: en una taula petita és el que
        # s'espera, i evita haver de triar columna abans de buscar.
        where = " WHERE " + " OR ".join(
            f"CAST({_cita(c)} AS TEXT) LIKE ?" for c in cols
        )
        params = [f"%{cerca.strip()}%"] * len(cols)
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM {_cita(taula)}{where}", params
        ).fetchone()[0]
        rows = c.execute(
            f"SELECT rowid AS _rowid, * FROM {_cita(taula)}{where} "
            f"LIMIT ? OFFSET ?",
            [*params, PER_PAGINA, pagina * PER_PAGINA],
        ).fetchall()
    return [dict(r) for r in rows], total


def executa(sql: str) -> dict[str, Any]:
    """Executa SQL lliure. Torna files si en dona, o quantes n'ha tocat."""
    t0 = time.perf_counter()
    with _conn() as c:
        cur = c.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(r) for r in cur.fetchmany(500)]
            return {"cols": cols, "rows": rows,
                    "ms": (time.perf_counter() - t0) * 1000}
        return {"tocades": cur.rowcount,
                "ms": (time.perf_counter() - t0) * 1000}


# --------------------------------------------------------------------------
# Peces d'interfície
# --------------------------------------------------------------------------
def avis(text: str, tipus: str = "ok") -> None:
    ui.notify(
        text,
        position="top-right",
        color={"ok": C["accent"], "err": C["danger"]}.get(tipus, C["accent"]),
        text_color="#fff",
        timeout=4000 if tipus == "err" else 2200,
    )


def boto(text: str, on_click, icon: str | None = None, tipus: str = "normal"):
    b = ui.button(text, on_click=on_click, icon=icon)
    if tipus == "primari":
        b.props(f'unelevated color=blue-6 no-caps')
    elif tipus == "perill":
        b.props('flat no-caps color=red-5')
    else:
        b.props('outline no-caps').style(f"color:{C['dim']};border-color:{C['line2']}")
    b.classes("cursor-pointer")
    return b


class Panel:
    """Tot l'estat del panell: quina taula, quina pestanya, quina pàgina."""

    def __init__(self) -> None:
        self.taula: str | None = None
        self.pestanya = "dades"
        self.pagina = 0
        self.cerca = ""
        self.cont_lateral = None
        self.cont_principal = None

    # ---------------- barra lateral ----------------
    def pinta_lateral(self) -> None:
        self.cont_lateral.clear()
        with self.cont_lateral:
            llista = taules()
            with ui.row().classes("w-full items-center justify-between px-1 pb-2"):
                ui.label("TAULES").style(
                    f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                ui.label(str(len(llista))).classes("badge")
            for t in llista:
                sel = t["nom"] == self.taula
                with ui.row().classes(
                    "taula-item w-full items-center justify-between px-3 py-2"
                ).props(f'data-sel={str(sel).lower()}').on(
                    "click", lambda _, n=t["nom"]: self.obre(n)
                ):
                    ui.label(t["nom"]).classes("mono text-sm").style(
                        f"color:{C['accent'] if sel else C['fg']}")
                    ui.label(f"{t['files']:,}").classes("badge")
            if not llista:
                ui.label("Cap taula encara").style(
                    f"color:{C['faint']};font-size:13px;padding:12px 4px")

            ui.separator().classes("hairline my-3")
            boto("Nova taula", self.dialeg_nova_taula, icon="add").classes("w-full")

    # ---------------- principal ----------------
    def obre(self, nom: str) -> None:
        self.taula = nom
        self.pagina = 0
        self.cerca = ""
        self.pinta_lateral()
        self.pinta_principal()

    def pinta_principal(self) -> None:
        self.cont_principal.clear()
        with self.cont_principal:
            if not self.taula:
                self.buit()
                return
            self.capcalera()
            if self.pestanya == "dades":
                self.vista_dades()
            elif self.pestanya == "estructura":
                self.vista_estructura()
            else:
                self.vista_sql()

    def buit(self) -> None:
        with ui.column().classes("w-full items-center justify-center gap-3").style(
            "min-height:60vh"
        ):
            ui.icon("database").style(f"font-size:44px;color:{C['line2']}")
            ui.label("Tria una taula").style(f"color:{C['dim']};font-size:15px")
            ui.label("O executa SQL directament des de la pestanya SQL.").style(
                f"color:{C['faint']};font-size:13px")

    def capcalera(self) -> None:
        with ui.row().classes("w-full items-center gap-2 px-5 pt-4 pb-3"):
            ui.label(self.taula).classes("mono text-lg").style(
                f"color:{C['fg']};font-weight:600")
            ui.space()
            for clau, etiqueta in (("dades", "Dades"), ("estructura", "Estructura"),
                                   ("sql", "SQL")):
                actiu = self.pestanya == clau
                ui.button(
                    etiqueta, on_click=lambda _, k=clau: self.canvia_pestanya(k)
                ).props("flat no-caps dense").classes("cursor-pointer").style(
                    f"color:{C['accent'] if actiu else C['dim']};"
                    f"font-weight:{600 if actiu else 400};"
                    f"border-bottom:2px solid {C['accent'] if actiu else 'transparent'};"
                    "border-radius:0;padding:2px 10px"
                )
        ui.separator().classes("hairline")

    def canvia_pestanya(self, k: str) -> None:
        self.pestanya = k
        self.pinta_principal()

    # ---------------- dades ----------------
    def vista_dades(self) -> None:
        dades, total = files(self.taula, self.pagina, self.cerca)
        cols_info = columnes(self.taula)

        with ui.row().classes("w-full items-center gap-3 px-5 py-3"):
            cerca = ui.input(placeholder="Filtrar files…").props(
                "outlined dense clearable"
            ).style("width:240px")
            cerca.on("keydown.enter", lambda: self.filtra(cerca.value or ""))
            boto("Filtrar", lambda: self.filtra(cerca.value or ""), icon="search")
            ui.space()
            ui.label(f"{total:,} files").style(f"color:{C['faint']};font-size:13px")
            boto("Nova fila", self.dialeg_nova_fila, icon="add", tipus="primari")

        if not dades:
            with ui.column().classes("w-full items-center py-14 gap-2"):
                ui.label("Cap fila" + (f" amb «{self.cerca}»" if self.cerca else "")
                         ).style(f"color:{C['dim']}")
            return

        noms = [c["name"] for c in cols_info]
        columns = [
            {"name": n, "label": n, "field": n, "align": "left", "sortable": True}
            for n in noms
        ]
        columns.append({"name": "_acc", "label": "", "field": "_acc", "align": "right"})

        with ui.element("div").classes("scroll-x"):
            taula_ui = ui.table(
                columns=columns, rows=dades, row_key="_rowid",
            ).classes("w-full").props("flat dense wrap-cells")
            taula_ui.style("background:transparent;min-width:640px")

        # Botó d'esborrar per fila. Es fa amb un slot perquè Quasar el pinti
        # dins de la cel·la, no en una columna a part que trencaria l'alineació.
        taula_ui.add_slot("body-cell-_acc", r'''
            <q-td :props="props" style="width:1%">
              <q-btn dense flat icon="delete_outline" size="sm"
                     color="grey-6" class="cursor-pointer"
                     @click="$parent.$emit('esborra', props.row)" />
            </q-td>
        ''')
        taula_ui.on("esborra", lambda e: self.confirma_esborrar(e.args))

        # Editar fent doble clic: és el gest que s'espera en una graella.
        taula_ui.on("rowDblclick", lambda e: self.dialeg_edita_fila(e.args[1]))

        pagines = max(1, -(-total // PER_PAGINA))
        if pagines > 1:
            with ui.row().classes("w-full items-center justify-center gap-3 py-3"):
                boto("Anterior", lambda: self.va_pagina(self.pagina - 1),
                     icon="chevron_left").set_enabled(self.pagina > 0)
                ui.label(f"{self.pagina + 1} / {pagines}").style(
                    f"color:{C['dim']};font-size:13px")
                boto("Següent", lambda: self.va_pagina(self.pagina + 1),
                     icon="chevron_right").set_enabled(self.pagina < pagines - 1)

        with ui.row().classes("px-5 pb-4"):
            ui.label("Doble clic en una fila per editar-la.").style(
                f"color:{C['faint']};font-size:12px")

    def filtra(self, text: str) -> None:
        self.cerca = text
        self.pagina = 0
        self.pinta_principal()

    def va_pagina(self, n: int) -> None:
        self.pagina = n
        self.pinta_principal()

    # ---------------- estructura ----------------
    def vista_estructura(self) -> None:
        with ui.column().classes("w-full gap-4 p-5"):
            with ui.element("div").classes("panel w-full p-4"):
                ui.label("SQL DE CREACIÓ").style(
                    f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                ui.label(ddl(self.taula)).classes("mono text-xs whitespace-pre-wrap mt-2"
                                                  ).style(f"color:{C['dim']}")

            with ui.element("div").classes("panel w-full"):
                with ui.row().classes("w-full items-center justify-between p-4 pb-3"):
                    ui.label("COLUMNES").style(
                        f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                    boto("Afegir columna", self.dialeg_nova_columna, icon="add")
                ui.separator().classes("hairline")
                for c in columnes(self.taula):
                    with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                        f"border-bottom:1px solid {C['line']}"
                    ):
                        ui.label(c["name"]).classes("mono text-sm").style(
                            f"color:{C['fg']};min-width:170px")
                        ui.label(c["type"] or "—").classes("chip-tipus")
                        ui.space()
                        if c["pk"]:
                            ui.label("PRIMARY KEY").classes("badge").style(
                                f"color:{C['accent']};border-color:{C['accent']}")
                        if c["notnull"]:
                            ui.label("NOT NULL").classes("badge")

            idx = indexs(self.taula)
            if idx:
                with ui.element("div").classes("panel w-full"):
                    ui.label("ÍNDEXS").classes("p-4 pb-3 block").style(
                        f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                    ui.separator().classes("hairline")
                    for i in idx:
                        with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                            f"border-bottom:1px solid {C['line']}"
                        ):
                            ui.label(i["nom"]).classes("mono text-sm").style(
                                f"color:{C['fg']}")
                            ui.label(", ".join(i["cols"])).classes("chip-tipus")
                            ui.space()
                            if i["unic"]:
                                ui.label("ÚNIC").classes("badge")

            with ui.row().classes("w-full justify-end pt-1"):
                boto("Esborrar taula", self.confirma_drop, icon="delete_outline",
                     tipus="perill")

    # ---------------- SQL ----------------
    def vista_sql(self) -> None:
        with ui.column().classes("w-full gap-3 p-5"):
            editor = ui.textarea(
                placeholder=f"SELECT * FROM {self.taula} LIMIT 10;"
            ).props("outlined").classes("w-full mono").style("min-height:150px")
            barra = ui.row().classes("gap-2 items-center")
            resultat = ui.column().classes("w-full")

            def corre() -> None:
                sql = (editor.value or "").strip()
                if not sql:
                    return
                resultat.clear()
                try:
                    r = executa(sql)
                except sqlite3.Error as e:
                    with resultat:
                        with ui.element("div").classes("panel w-full p-4").style(
                            f"border-color:{C['danger']}"
                        ):
                            ui.label(str(e)).classes("mono text-sm").style(
                                f"color:{C['danger']}")
                    return
                self.pinta_lateral()
                with resultat:
                    if "rows" in r:
                        ui.label(
                            f"{len(r['rows'])} files · {r['ms']:.0f} ms"
                            + (" · només es mostren les 500 primeres"
                               if len(r["rows"]) == 500 else "")
                        ).style(f"color:{C['faint']};font-size:12px")
                        if r["rows"]:
                            with ui.element("div").classes("scroll-x"):
                                ui.table(
                                    columns=[{"name": c, "label": c, "field": c,
                                              "align": "left"} for c in r["cols"]],
                                    rows=r["rows"],
                                ).classes("w-full").props("flat dense wrap-cells")
                    else:
                        ui.label(
                            f"{r['tocades']} files afectades · {r['ms']:.0f} ms"
                        ).style(f"color:{C['ok']};font-size:13px")
                        avis("Consulta executada")

            with barra:
                boto("Executar", corre, icon="play_arrow", tipus="primari")
                ui.label("Ctrl+Enter").style(f"color:{C['faint']};font-size:12px")
            editor.on("keydown.ctrl.enter", corre)

    # ---------------- diàlegs ----------------
    def _dialeg(self, titol: str):
        d = ui.dialog()
        with d, ui.card().classes("panel").style(
            f"background:{C['surface']};min-width:420px;padding:20px"
        ):
            ui.label(titol).style(f"color:{C['fg']};font-size:16px;font-weight:600")
        return d

    def dialeg_nova_fila(self) -> None:
        cols = [c for c in columnes(self.taula)]
        d = self._dialeg(f"Nova fila a {self.taula}")
        camps: dict[str, Any] = {}
        with d, d.default_slot.children[0]:
            for c in cols:
                camps[c["name"]] = ui.input(
                    label=f"{c['name']}  ({c['type'] or 'TEXT'})"
                ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Afegir", lambda: self._insereix(camps, d), tipus="primari")
        d.open()

    def _insereix(self, camps: dict, d) -> None:
        noms = [n for n, c in camps.items() if (c.value or "") != ""]
        if not noms:
            avis("Omple almenys un camp", "err")
            return
        vals = [camps[n].value for n in noms]
        sql = (f"INSERT INTO {_cita(self.taula)} "
               f"({', '.join(_cita(n) for n in noms)}) "
               f"VALUES ({', '.join('?' * len(noms))})")
        try:
            with _conn() as c:
                c.execute(sql, vals)
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis("Fila afegida")
        self.pinta_lateral()
        self.pinta_principal()

    def dialeg_edita_fila(self, fila: dict) -> None:
        cols = columnes(self.taula)
        d = self._dialeg("Editar fila")
        camps: dict[str, Any] = {}
        with d, d.default_slot.children[0]:
            for c in cols:
                n = c["name"]
                camps[n] = ui.input(
                    label=n, value="" if fila.get(n) is None else str(fila.get(n))
                ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Desar", lambda: self._actualitza(camps, fila, d), tipus="primari")
        d.open()

    def _actualitza(self, camps: dict, fila: dict, d) -> None:
        sets = ", ".join(f"{_cita(n)} = ?" for n in camps)
        vals = [c.value for c in camps.values()]
        try:
            with _conn() as c:
                # Per rowid i no per clau primària: funciona encara que la
                # taula no en tingui, i encara que l'usuari editi la clau.
                c.execute(
                    f"UPDATE {_cita(self.taula)} SET {sets} WHERE rowid = ?",
                    [*vals, fila["_rowid"]],
                )
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis("Fila desada")
        self.pinta_principal()

    def confirma_esborrar(self, fila: dict) -> None:
        d = self._dialeg("Esborrar aquesta fila?")
        with d, d.default_slot.children[0]:
            ui.label("No es pot desfer.").style(f"color:{C['dim']};font-size:13px")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Esborrar", lambda: self._esborra(fila, d), tipus="perill")
        d.open()

    def _esborra(self, fila: dict, d) -> None:
        try:
            with _conn() as c:
                c.execute(f"DELETE FROM {_cita(self.taula)} WHERE rowid = ?",
                          (fila["_rowid"],))
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis("Fila esborrada")
        self.pinta_lateral()
        self.pinta_principal()

    def dialeg_nova_columna(self) -> None:
        d = self._dialeg(f"Afegir columna a {self.taula}")
        with d, d.default_slot.children[0]:
            nom = ui.input(label="Nom").props("outlined dense").classes("w-full")
            tipus = ui.select(
                ["TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"], value="TEXT",
                label="Tipus",
            ).props("outlined dense").classes("w-full")
            defecte = ui.input(label="Valor per defecte (opcional)").props(
                "outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Afegir", lambda: self._afegeix_columna(
                    nom.value, tipus.value, defecte.value, d), tipus="primari")
        d.open()

    def _afegeix_columna(self, nom: str, tipus: str, defecte: str, d) -> None:
        if not _nom_valid(nom):
            avis("Nom no vàlid: lletres, números i guions baixos", "err")
            return
        sql = f"ALTER TABLE {_cita(self.taula)} ADD COLUMN {_cita(nom)} {tipus}"
        if defecte:
            sql += " DEFAULT ?"
        try:
            with _conn() as c:
                c.execute(sql, (defecte,) if defecte else ())
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis(f"Columna {nom} afegida")
        self.pinta_principal()

    def dialeg_nova_taula(self) -> None:
        d = self._dialeg("Nova taula")
        files_col: list[dict] = []
        with d, d.default_slot.children[0]:
            nom = ui.input(label="Nom de la taula").props(
                "outlined dense").classes("w-full")
            ui.label("COLUMNES").style(
                f"color:{C['faint']};font-size:11px;letter-spacing:.09em;"
                "font-weight:600;padding-top:8px")
            cont = ui.column().classes("w-full gap-2")

            def afegeix_fila(nom_def: str = "", tipus_def: str = "TEXT",
                             pk: bool = False) -> None:
                with cont:
                    with ui.row().classes("w-full items-center gap-2") as fila:
                        n = ui.input(placeholder="nom").props(
                            "outlined dense").style("flex:1")
                        n.value = nom_def
                        t = ui.select(["TEXT", "INTEGER", "REAL", "BLOB"],
                                      value=tipus_def).props(
                            "outlined dense").style("width:110px")
                        k = ui.checkbox("PK", value=pk).props("dense")
                        ui.button(icon="close", on_click=lambda: (
                            fila.delete(), files_col.remove(ref))
                        ).props("flat dense round size=sm color=grey-7").classes(
                            "cursor-pointer")
                        ref = {"n": n, "t": t, "k": k}
                        files_col.append(ref)

            afegeix_fila("id", "INTEGER", True)
            afegeix_fila("nom", "TEXT")
            boto("Afegir columna", lambda: afegeix_fila(), icon="add")

            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Crear", lambda: self._crea_taula(nom.value, files_col, d),
                     tipus="primari")
        d.open()

    def _crea_taula(self, nom: str, cols: list[dict], d) -> None:
        if not _nom_valid(nom):
            avis("Nom de taula no vàlid", "err")
            return
        definicions = []
        for c in cols:
            cn = (c["n"].value or "").strip()
            if not cn:
                continue
            if not _nom_valid(cn):
                avis(f"Nom de columna no vàlid: {cn}", "err")
                return
            definicions.append(
                f"{_cita(cn)} {c['t'].value}" + (" PRIMARY KEY" if c["k"].value else "")
            )
        if not definicions:
            avis("Cal almenys una columna", "err")
            return
        try:
            with _conn() as c:
                c.execute(f"CREATE TABLE {_cita(nom)} ({', '.join(definicions)})")
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis(f"Taula {nom} creada")
        self.obre(nom)

    def confirma_drop(self) -> None:
        d = self._dialeg(f"Esborrar la taula {self.taula}?")
        with d, d.default_slot.children[0]:
            ui.label("S'esborren totes les files i no es pot desfer.").style(
                f"color:{C['dim']};font-size:13px")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                boto("Cancel·lar", d.close)
                boto("Esborrar la taula", lambda: self._drop(d), tipus="perill")
        d.open()

    def _drop(self, d) -> None:
        try:
            with _conn() as c:
                c.execute(f"DROP TABLE {_cita(self.taula)}")
        except sqlite3.Error as e:
            avis(str(e), "err")
            return
        d.close()
        avis("Taula esborrada")
        self.taula = None
        self.pinta_lateral()
        self.pinta_principal()


def _nom_valid(nom: str) -> bool:
    """Els noms nous es restringeixen, encara que _cita() ja els protegeixi.

    Un nom amb cometes o espais és legal a SQLite però fa la vida impossible
    després; val més no deixar-los crear.
    """
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", (nom or "").strip()))


# --------------------------------------------------------------------------
# Pàgina
# --------------------------------------------------------------------------
def _estat_bd() -> dict[str, Any]:
    llista = taules()
    return {"taules": len(llista), "files": sum(t["files"] for t in llista),
            "kb": memory.stats()["dbBytes"] / 1024}


class _Porta(BaseHTTPMiddleware):
    """Deixa passar cap a /entrar i cap als fitxers estàtics de NiceGUI.

    La resta del panell demana haver posat la contrasenya. Va com a middleware
    i no com a dependència perquè el WebSocket de NiceGUI no passa per les
    dependències de FastAPI.
    """

    async def dispatch(self, request, call_next):
        cami = request.url.path
        if not cami.startswith(PREFIX) or cami.startswith(f"{PREFIX}/_nicegui"):
            return await call_next(request)
        if cami.rstrip("/") == f"{PREFIX}/entrar":
            return await call_next(request)
        if not nicegui_app.storage.user.get("dins"):
            return RedirectResponse(f"{PREFIX}/entrar")
        return await call_next(request)


def construeix(mount_path: str = "/admin") -> None:
    """Munta el panell dins de l'app de FastAPI que li passi main.py."""
    global PREFIX
    PREFIX = mount_path.rstrip("/")
    nicegui_app.add_middleware(_Porta)

    @ui.page("/entrar")
    def entrar() -> None:
        ui.add_head_html(f"<style>{CSS}</style>")
        ui.dark_mode().enable()

        def prova() -> None:
            # compare_digest per no filtrar la contrasenya pel temps que triga
            # a fallar la comparació.
            if secrets.compare_digest(clau.value or "", CONTRASENYA):
                nicegui_app.storage.user["dins"] = True
                ui.navigate.to("/")
            else:
                error.set_visibility(True)

        with ui.column().classes("w-full items-center justify-center").style(
            "min-height:100vh"
        ):
            with ui.element("div").classes("panel").style(
                f"background:{C['surface']};padding:28px;width:340px"
            ):
                with ui.row().classes("items-center gap-2 pb-1"):
                    ui.icon("storage").style(f"color:{C['accent']};font-size:20px")
                    ui.label("Bonsai · base de dades").style(
                        f"color:{C['fg']};font-weight:600")
                ui.label("Accés d'administració").style(
                    f"color:{C['faint']};font-size:13px;padding-bottom:14px")
                clau = ui.input(label="Contrasenya", password=True).props(
                    "outlined dense autofocus").classes("w-full")
                clau.on("keydown.enter", prova)
                error = ui.label("Contrasenya incorrecta").style(
                    f"color:{C['danger']};font-size:13px;padding-top:8px")
                error.set_visibility(False)
                with ui.row().classes("w-full pt-4"):
                    boto("Entrar", prova, tipus="primari").classes("w-full")

    @ui.page("/")
    def pagina() -> None:
        ui.add_head_html(f"<style>{CSS}</style>")
        ui.query("body").style(f"background:{C['bg']}")
        ui.dark_mode().enable()

        p = Panel()
        est = _estat_bd()

        def surt() -> None:
            nicegui_app.storage.user.clear()
            ui.navigate.to("/entrar")

        # Capçalera
        with ui.row().classes("w-full items-center gap-4 px-6 py-3").style(
            f"background:{C['surface']};border-bottom:1px solid {C['line']}"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.icon("storage").style(f"color:{C['accent']};font-size:20px")
                ui.label("Bonsai · base de dades").style(
                    f"color:{C['fg']};font-weight:600;letter-spacing:-.01em")
            ui.space()
            for valor, etiqueta in (
                (f"{est['taules']}", "taules"),
                (f"{est['files']:,}", "files"),
                (f"{est['kb']:.0f} KB", "mida"),
            ):
                with ui.column().classes("items-end gap-0"):
                    ui.label(valor).style(
                        f"color:{C['fg']};font-size:14px;font-weight:600;"
                        "font-variant-numeric:tabular-nums;line-height:1.2")
                    ui.label(etiqueta).style(
                        f"color:{C['faint']};font-size:10px;letter-spacing:.07em;"
                        "text-transform:uppercase")
            ui.element("div").style(f"width:1px;height:26px;background:{C['line']}")
            ui.link("Provar", "/provar").style(
                f"color:{C['dim']};font-size:13px;text-decoration:none").classes(
                "cursor-pointer hover:underline")
            ui.button(icon="logout", on_click=surt).props(
                "flat dense round size=sm color=grey-7").classes("cursor-pointer"
                ).tooltip("Sortir")

        # Cos
        with ui.row().classes("cos w-full gap-0 items-stretch").style(
            "min-height:calc(100vh - 57px)"
        ):
            with ui.column().classes("lateral gap-0 p-3").style(
                f"width:250px;flex:none;border-right:1px solid {C['line']};"
                f"background:{C['surface']}"
            ):
                p.cont_lateral = ui.column().classes("w-full gap-1")
            p.cont_principal = ui.column().classes("principal gap-0").style(
                "flex:1;min-width:0")

        p.pinta_lateral()
        # Obre la primera taula: el cas normal és que només n'hi hagi una.
        primera = taules()
        if primera:
            p.obre(primera[0]["nom"])
        else:
            p.pinta_principal()
