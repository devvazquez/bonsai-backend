"""Database admin panel, mounted inside the same FastAPI app.

Replaces DbGate: browse tables, edit rows, create tables and columns, run SQL,
but integrated into the backend — one container, one port, one login.

Built with NiceGUI, which serves Vue and Quasar from its own package, so it
works on a VPS with no outbound internet.
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

# This is full SQL access to the database, so with no password defined the
# panel is not mounted at all: better absent than open.
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Prefix where the panel is mounted, set by build(). Only the middleware needs
# it (it sees the full path); inside pages ui.navigate.to() adds it already.
PREFIX = "/admin"

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
C = {
    "bg": "#0B0B10",        # background, near black
    "surface": "#121218",   # panels
    "card": "#1A1A21",      # raised
    "line": "#242430",      # borders
    "line2": "#33333F",     # highlighted borders
    "fg": "#F8FAFC",
    "dim": "#A1A1B5",
    "faint": "#6C6C82",
    "accent": "#3B82F6",    # blue
    "accent2": "#60A5FA",
    "danger": "#EF4444",
    "ok": "#34D399",
    "warn": "#FBBF24",
}

# Rows per page when browsing a table: enough to work with without turning the
# page into a download of the whole database.
PER_PAGE = 50

CSS = f"""
:root {{
  --bg:{C['bg']}; --surface:{C['surface']}; --card:{C['card']};
  --line:{C['line']}; --line2:{C['line2']}; --fg:{C['fg']};
  --dim:{C['dim']}; --faint:{C['faint']}; --accent:{C['accent']};
  --danger:{C['danger']};
}}
body, .nicegui-content {{ background:var(--bg); color:var(--fg); }}
.q-page, .nicegui-content {{ padding:0 !important; }}

/* System font stack to avoid depending on Google Fonts, and a real monospace
   for the data, which is where alignment matters. */
body {{ font-family: ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
        -webkit-font-smoothing:antialiased; }}
.mono {{ font-family: ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace; }}

.panel {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; }}
.hairline {{ border-color:var(--line) !important; }}

/* Sidebar */
.table-item {{ border-radius:8px; cursor:pointer; transition:background .15s,color .15s; }}
.table-item:hover {{ background:var(--card); }}
.table-item[data-sel="true"] {{ background:rgba(59,130,246,.14);
  box-shadow:inset 2px 0 0 var(--accent); }}

/* Data table */
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

/* Accessibility: always-visible focus, and honour reduced-motion requests */
*:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
@media (prefers-reduced-motion:reduce) {{
  * {{ animation:none !important; transition:none !important; }}
}}

/* Dialogs are q-card, which brings its own light border */
.q-dialog .q-card {{ border:1px solid var(--line2) !important;
  background:var(--surface) !important; }}

/* Data can be wider than the screen: scroll the table, not the whole page. */
.scroll-x {{ width:100%; overflow-x:auto; }}

/* Mobile: the sidebar moves on top instead of stealing width */
@media (max-width:860px) {{
  /* NiceGUI sets flex-wrap:nowrap on .nicegui-row, we have to beat it */
  .body-row {{ flex-wrap:wrap !important; }}
  .sidebar {{ width:100% !important; border-right:none !important;
    border-bottom:1px solid var(--line);
    /* take only the height it needs, not the whole screen */
    align-self:flex-start; }}
  /* the inline flex:1 (basis 0) collapses the column to zero width once
     wrapping, so give it the whole row */
  .main {{ flex:1 1 100% !important; }}
}}

.badge {{ font-size:11px; padding:1px 7px; border-radius:20px;
  border:1px solid var(--line2); color:var(--dim); font-variant-numeric:tabular-nums; }}
.chip-type {{ font-size:10px; letter-spacing:.05em; color:var(--faint);
  text-transform:uppercase; }}
"""


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(memory.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _quote(name: str) -> str:
    """Quote an identifier for SQL.

    Table and column names cannot be bound as parameters, so they go inline;
    doubling the quotes is what stops `a"; DROP TABLE x --` from becoming a
    second statement.
    """
    return '"' + str(name).replace('"', '""') + '"'


def tables() -> list[dict[str, Any]]:
    with _conn() as c:
        names = [
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out = []
        for n in names:
            try:
                total = c.execute(f"SELECT COUNT(*) FROM {_quote(n)}").fetchone()[0]
            except sqlite3.Error:
                total = 0
            out.append({"name": n, "rows": total})
    return out


def columns(table: str) -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(f"PRAGMA table_info({_quote(table)})")]


def indexes(table: str) -> list[dict[str, Any]]:
    with _conn() as c:
        out = []
        for r in c.execute(f"PRAGMA index_list({_quote(table)})"):
            cols = [
                x["name"]
                for x in c.execute(f"PRAGMA index_info({_quote(r['name'])})")
            ]
            out.append({"name": r["name"], "unique": bool(r["unique"]), "cols": cols})
    return out


def ddl(table: str) -> str:
    with _conn() as c:
        r = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    raw = (r["sql"] if r else "") or ""
    # CREATE TABLE is stored exactly as written, indentation included, which
    # looks ragged here: re-indent to two spaces.
    lines = raw.splitlines()
    return "\n".join(
        [lines[0].strip()] + ["  " + l.strip() for l in lines[1:] if l.strip()]
    ) if lines else ""


def rows(table: str, page: int, search: str = "") -> tuple[list[dict], int]:
    cols = [c["name"] for c in columns(table)]
    where, params = "", []
    if search.strip() and cols:
        # Search every column at once: that is what one expects on a small
        # table, and it saves picking a column before searching.
        where = " WHERE " + " OR ".join(
            f"CAST({_quote(c)} AS TEXT) LIKE ?" for c in cols
        )
        params = [f"%{search.strip()}%"] * len(cols)
    with _conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) FROM {_quote(table)}{where}", params
        ).fetchone()[0]
        records = c.execute(
            f"SELECT rowid AS _rowid, * FROM {_quote(table)}{where} "
            f"LIMIT ? OFFSET ?",
            [*params, PER_PAGE, page * PER_PAGE],
        ).fetchall()
    return [dict(r) for r in records], total


def run_sql(sql: str) -> dict[str, Any]:
    """Run free-form SQL. Returns rows if it yields any, else the row count."""
    t0 = time.perf_counter()
    with _conn() as c:
        cur = c.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            result = [dict(r) for r in cur.fetchmany(500)]
            return {"cols": cols, "rows": result,
                    "ms": (time.perf_counter() - t0) * 1000}
        return {"affected": cur.rowcount,
                "ms": (time.perf_counter() - t0) * 1000}


# --------------------------------------------------------------------------
# UI pieces
# --------------------------------------------------------------------------
def notify(text: str, kind: str = "ok") -> None:
    ui.notify(
        text,
        position="top-right",
        color={"ok": C["accent"], "err": C["danger"]}.get(kind, C["accent"]),
        text_color="#fff",
        timeout=4000 if kind == "err" else 2200,
    )


def button(text: str, on_click, icon: str | None = None, kind: str = "normal"):
    b = ui.button(text, on_click=on_click, icon=icon)
    if kind == "primary":
        b.props(f'unelevated color=blue-6 no-caps')
    elif kind == "danger":
        b.props('flat no-caps color=red-5')
    else:
        b.props('outline no-caps').style(f"color:{C['dim']};border-color:{C['line2']}")
    b.classes("cursor-pointer")
    return b


class Panel:
    """All the panel state: which table, which tab, which page."""

    def __init__(self) -> None:
        self.table: str | None = None
        self.tab = "data"
        self.page = 0
        self.search = ""
        self.sidebar_container = None
        self.main_container = None

    # ---------------- sidebar ----------------
    def draw_sidebar(self) -> None:
        self.sidebar_container.clear()
        with self.sidebar_container:
            listing = tables()
            with ui.row().classes("w-full items-center justify-between px-1 pb-2"):
                ui.label("TABLES").style(
                    f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                ui.label(str(len(listing))).classes("badge")
            for t in listing:
                sel = t["name"] == self.table
                with ui.row().classes(
                    "table-item w-full items-center justify-between px-3 py-2"
                ).props(f'data-sel={str(sel).lower()}').on(
                    "click", lambda _, n=t["name"]: self.open(n)
                ):
                    ui.label(t["name"]).classes("mono text-sm").style(
                        f"color:{C['accent'] if sel else C['fg']}")
                    ui.label(f"{t['rows']:,}").classes("badge")
            if not listing:
                ui.label("No tables yet").style(
                    f"color:{C['faint']};font-size:13px;padding:12px 4px")

            ui.separator().classes("hairline my-3")
            button("New table", self.dialog_new_table, icon="add").classes("w-full")

    # ---------------- main ----------------
    def open(self, name: str) -> None:
        self.table = name
        self.page = 0
        self.search = ""
        self.draw_sidebar()
        self.draw_main()

    def draw_main(self) -> None:
        self.main_container.clear()
        with self.main_container:
            if not self.table:
                self.empty()
                return
            self.header()
            if self.tab == "data":
                self.data_view()
            elif self.tab == "structure":
                self.structure_view()
            else:
                self.sql_view()

    def empty(self) -> None:
        with ui.column().classes("w-full items-center justify-center gap-3").style(
            "min-height:60vh"
        ):
            ui.icon("database").style(f"font-size:44px;color:{C['line2']}")
            ui.label("Pick a table").style(f"color:{C['dim']};font-size:15px")
            ui.label("Or run SQL directly from the SQL tab.").style(
                f"color:{C['faint']};font-size:13px")

    def header(self) -> None:
        with ui.row().classes("w-full items-center gap-2 px-5 pt-4 pb-3"):
            ui.label(self.table).classes("mono text-lg").style(
                f"color:{C['fg']};font-weight:600")
            ui.space()
            for key, label in (("data", "Data"), ("structure", "Structure"),
                               ("sql", "SQL")):
                active = self.tab == key
                ui.button(
                    label, on_click=lambda _, k=key: self.switch_tab(k)
                ).props("flat no-caps dense").classes("cursor-pointer").style(
                    f"color:{C['accent'] if active else C['dim']};"
                    f"font-weight:{600 if active else 400};"
                    f"border-bottom:2px solid {C['accent'] if active else 'transparent'};"
                    "border-radius:0;padding:2px 10px"
                )
        ui.separator().classes("hairline")

    def switch_tab(self, k: str) -> None:
        self.tab = k
        self.draw_main()

    # ---------------- data ----------------
    def data_view(self) -> None:
        data, total = rows(self.table, self.page, self.search)
        cols_info = columns(self.table)

        with ui.row().classes("w-full items-center gap-3 px-5 py-3"):
            search = ui.input(placeholder="Filter rows…").props(
                "outlined dense clearable"
            ).style("width:240px")
            search.on("keydown.enter", lambda: self.filter(search.value or ""))
            button("Filter", lambda: self.filter(search.value or ""), icon="search")
            ui.space()
            ui.label(f"{total:,} rows").style(f"color:{C['faint']};font-size:13px")
            button("New row", self.dialog_new_row, icon="add", kind="primary")

        if not data:
            with ui.column().classes("w-full items-center py-14 gap-2"):
                ui.label("No rows" + (f" matching «{self.search}»" if self.search else "")
                         ).style(f"color:{C['dim']}")
            return

        names = [c["name"] for c in cols_info]
        col_defs = [
            {"name": n, "label": n, "field": n, "align": "left", "sortable": True}
            for n in names
        ]
        col_defs.append({"name": "_acc", "label": "", "field": "_acc", "align": "right"})

        with ui.element("div").classes("scroll-x"):
            table_ui = ui.table(
                columns=col_defs, rows=data, row_key="_rowid",
            ).classes("w-full").props("flat dense wrap-cells")
            table_ui.style("background:transparent;min-width:640px")

        # Per-row delete button, via a slot so Quasar paints it inside the cell
        # instead of a separate column that would break the alignment.
        table_ui.add_slot("body-cell-_acc", r'''
            <q-td :props="props" style="width:1%">
              <q-btn dense flat icon="delete_outline" size="sm"
                     color="grey-6" class="cursor-pointer"
                     @click="$parent.$emit('remove', props.row)" />
            </q-td>
        ''')
        table_ui.on("remove", lambda e: self.confirm_delete(e.args))

        # Double click to edit: the expected gesture in a grid.
        table_ui.on("rowDblclick", lambda e: self.dialog_edit_row(e.args[1]))

        pages = max(1, -(-total // PER_PAGE))
        if pages > 1:
            with ui.row().classes("w-full items-center justify-center gap-3 py-3"):
                button("Previous", lambda: self.go_page(self.page - 1),
                       icon="chevron_left").set_enabled(self.page > 0)
                ui.label(f"{self.page + 1} / {pages}").style(
                    f"color:{C['dim']};font-size:13px")
                button("Next", lambda: self.go_page(self.page + 1),
                       icon="chevron_right").set_enabled(self.page < pages - 1)

        with ui.row().classes("px-5 pb-4"):
            ui.label("Double click a row to edit it.").style(
                f"color:{C['faint']};font-size:12px")

    def filter(self, text: str) -> None:
        self.search = text
        self.page = 0
        self.draw_main()

    def go_page(self, n: int) -> None:
        self.page = n
        self.draw_main()

    # ---------------- structure ----------------
    def structure_view(self) -> None:
        with ui.column().classes("w-full gap-4 p-5"):
            with ui.element("div").classes("panel w-full p-4"):
                ui.label("CREATE SQL").style(
                    f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                ui.label(ddl(self.table)).classes("mono text-xs whitespace-pre-wrap mt-2"
                                                  ).style(f"color:{C['dim']}")

            with ui.element("div").classes("panel w-full"):
                with ui.row().classes("w-full items-center justify-between p-4 pb-3"):
                    ui.label("COLUMNS").style(
                        f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                    button("Add column", self.dialog_new_column, icon="add")
                ui.separator().classes("hairline")
                for c in columns(self.table):
                    with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                        f"border-bottom:1px solid {C['line']}"
                    ):
                        ui.label(c["name"]).classes("mono text-sm").style(
                            f"color:{C['fg']};min-width:170px")
                        ui.label(c["type"] or "—").classes("chip-type")
                        ui.space()
                        if c["pk"]:
                            ui.label("PRIMARY KEY").classes("badge").style(
                                f"color:{C['accent']};border-color:{C['accent']}")
                        if c["notnull"]:
                            ui.label("NOT NULL").classes("badge")

            idx = indexes(self.table)
            if idx:
                with ui.element("div").classes("panel w-full"):
                    ui.label("INDEXES").classes("p-4 pb-3 block").style(
                        f"color:{C['faint']};font-size:11px;letter-spacing:.09em;font-weight:600")
                    ui.separator().classes("hairline")
                    for i in idx:
                        with ui.row().classes("w-full items-center gap-3 px-4 py-3").style(
                            f"border-bottom:1px solid {C['line']}"
                        ):
                            ui.label(i["name"]).classes("mono text-sm").style(
                                f"color:{C['fg']}")
                            ui.label(", ".join(i["cols"])).classes("chip-type")
                            ui.space()
                            if i["unique"]:
                                ui.label("UNIQUE").classes("badge")

            with ui.row().classes("w-full justify-end pt-1"):
                button("Drop table", self.confirm_drop, icon="delete_outline",
                       kind="danger")

    # ---------------- SQL ----------------
    def sql_view(self) -> None:
        with ui.column().classes("w-full gap-3 p-5"):
            editor = ui.textarea(
                placeholder=f"SELECT * FROM {self.table} LIMIT 10;"
            ).props("outlined").classes("w-full mono").style("min-height:150px")
            bar = ui.row().classes("gap-2 items-center")
            output = ui.column().classes("w-full")

            def run() -> None:
                sql = (editor.value or "").strip()
                if not sql:
                    return
                output.clear()
                try:
                    r = run_sql(sql)
                except sqlite3.Error as e:
                    with output:
                        with ui.element("div").classes("panel w-full p-4").style(
                            f"border-color:{C['danger']}"
                        ):
                            ui.label(str(e)).classes("mono text-sm").style(
                                f"color:{C['danger']}")
                    return
                self.draw_sidebar()
                with output:
                    if "rows" in r:
                        ui.label(
                            f"{len(r['rows'])} rows · {r['ms']:.0f} ms"
                            + (" · showing only the first 500"
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
                            f"{r['affected']} rows affected · {r['ms']:.0f} ms"
                        ).style(f"color:{C['ok']};font-size:13px")
                        notify("Query executed")

            with bar:
                button("Run", run, icon="play_arrow", kind="primary")
                ui.label("Ctrl+Enter").style(f"color:{C['faint']};font-size:12px")
            editor.on("keydown.ctrl.enter", run)

    # ---------------- dialogs ----------------
    def _dialog(self, title: str):
        d = ui.dialog()
        with d, ui.card().classes("panel").style(
            f"background:{C['surface']};min-width:420px;padding:20px"
        ):
            ui.label(title).style(f"color:{C['fg']};font-size:16px;font-weight:600")
        return d

    def dialog_new_row(self) -> None:
        cols = [c for c in columns(self.table)]
        d = self._dialog(f"New row in {self.table}")
        fields: dict[str, Any] = {}
        with d, d.default_slot.children[0]:
            for c in cols:
                fields[c["name"]] = ui.input(
                    label=f"{c['name']}  ({c['type'] or 'TEXT'})"
                ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Add", lambda: self._insert(fields, d), kind="primary")
        d.open()

    def _insert(self, fields: dict, d) -> None:
        names = [n for n, c in fields.items() if (c.value or "") != ""]
        if not names:
            notify("Fill in at least one field", "err")
            return
        vals = [fields[n].value for n in names]
        sql = (f"INSERT INTO {_quote(self.table)} "
               f"({', '.join(_quote(n) for n in names)}) "
               f"VALUES ({', '.join('?' * len(names))})")
        try:
            with _conn() as c:
                c.execute(sql, vals)
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify("Row added")
        self.draw_sidebar()
        self.draw_main()

    def dialog_edit_row(self, row: dict) -> None:
        cols = columns(self.table)
        d = self._dialog("Edit row")
        fields: dict[str, Any] = {}
        with d, d.default_slot.children[0]:
            for c in cols:
                n = c["name"]
                fields[n] = ui.input(
                    label=n, value="" if row.get(n) is None else str(row.get(n))
                ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Save", lambda: self._update(fields, row, d), kind="primary")
        d.open()

    def _update(self, fields: dict, row: dict, d) -> None:
        sets = ", ".join(f"{_quote(n)} = ?" for n in fields)
        vals = [c.value for c in fields.values()]
        try:
            with _conn() as c:
                # By rowid, not primary key: works even if the table has none,
                # and even if the user edits the key itself.
                c.execute(
                    f"UPDATE {_quote(self.table)} SET {sets} WHERE rowid = ?",
                    [*vals, row["_rowid"]],
                )
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify("Row saved")
        self.draw_main()

    def confirm_delete(self, row: dict) -> None:
        d = self._dialog("Delete this row?")
        with d, d.default_slot.children[0]:
            ui.label("This cannot be undone.").style(f"color:{C['dim']};font-size:13px")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Delete", lambda: self._delete(row, d), kind="danger")
        d.open()

    def _delete(self, row: dict, d) -> None:
        try:
            with _conn() as c:
                c.execute(f"DELETE FROM {_quote(self.table)} WHERE rowid = ?",
                          (row["_rowid"],))
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify("Row deleted")
        self.draw_sidebar()
        self.draw_main()

    def dialog_new_column(self) -> None:
        d = self._dialog(f"Add column to {self.table}")
        with d, d.default_slot.children[0]:
            name = ui.input(label="Name").props("outlined dense").classes("w-full")
            col_type = ui.select(
                ["TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"], value="TEXT",
                label="Type",
            ).props("outlined dense").classes("w-full")
            default = ui.input(label="Default value (optional)").props(
                "outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Add", lambda: self._add_column(
                    name.value, col_type.value, default.value, d), kind="primary")
        d.open()

    def _add_column(self, name: str, col_type: str, default: str, d) -> None:
        if not _valid_name(name):
            notify("Invalid name: letters, digits and underscores", "err")
            return
        sql = f"ALTER TABLE {_quote(self.table)} ADD COLUMN {_quote(name)} {col_type}"
        if default:
            sql += " DEFAULT ?"
        try:
            with _conn() as c:
                c.execute(sql, (default,) if default else ())
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify(f"Column {name} added")
        self.draw_main()

    def dialog_new_table(self) -> None:
        d = self._dialog("New table")
        col_rows: list[dict] = []
        with d, d.default_slot.children[0]:
            name = ui.input(label="Table name").props(
                "outlined dense").classes("w-full")
            ui.label("COLUMNS").style(
                f"color:{C['faint']};font-size:11px;letter-spacing:.09em;"
                "font-weight:600;padding-top:8px")
            cont = ui.column().classes("w-full gap-2")

            def add_row(name_def: str = "", type_def: str = "TEXT",
                        pk: bool = False) -> None:
                with cont:
                    with ui.row().classes("w-full items-center gap-2") as row:
                        n = ui.input(placeholder="name").props(
                            "outlined dense").style("flex:1")
                        n.value = name_def
                        t = ui.select(["TEXT", "INTEGER", "REAL", "BLOB"],
                                      value=type_def).props(
                            "outlined dense").style("width:110px")
                        k = ui.checkbox("PK", value=pk).props("dense")
                        ui.button(icon="close", on_click=lambda: (
                            row.delete(), col_rows.remove(ref))
                        ).props("flat dense round size=sm color=grey-7").classes(
                            "cursor-pointer")
                        ref = {"n": n, "t": t, "k": k}
                        col_rows.append(ref)

            add_row("id", "INTEGER", True)
            add_row("name", "TEXT")
            button("Add column", lambda: add_row(), icon="add")

            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Create", lambda: self._create_table(name.value, col_rows, d),
                       kind="primary")
        d.open()

    def _create_table(self, name: str, cols: list[dict], d) -> None:
        if not _valid_name(name):
            notify("Invalid table name", "err")
            return
        definitions = []
        for c in cols:
            cn = (c["n"].value or "").strip()
            if not cn:
                continue
            if not _valid_name(cn):
                notify(f"Invalid column name: {cn}", "err")
                return
            definitions.append(
                f"{_quote(cn)} {c['t'].value}" + (" PRIMARY KEY" if c["k"].value else "")
            )
        if not definitions:
            notify("At least one column is required", "err")
            return
        try:
            with _conn() as c:
                c.execute(f"CREATE TABLE {_quote(name)} ({', '.join(definitions)})")
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify(f"Table {name} created")
        self.open(name)

    def confirm_drop(self) -> None:
        d = self._dialog(f"Drop table {self.table}?")
        with d, d.default_slot.children[0]:
            ui.label("Every row is deleted and this cannot be undone.").style(
                f"color:{C['dim']};font-size:13px")
            with ui.row().classes("w-full justify-end gap-2 pt-3"):
                button("Cancel", d.close)
                button("Drop table", lambda: self._drop(d), kind="danger")
        d.open()

    def _drop(self, d) -> None:
        try:
            with _conn() as c:
                c.execute(f"DROP TABLE {_quote(self.table)}")
        except sqlite3.Error as e:
            notify(str(e), "err")
            return
        d.close()
        notify("Table dropped")
        self.table = None
        self.draw_sidebar()
        self.draw_main()


def _valid_name(name: str) -> bool:
    """Restrict new names even though _quote() already protects them.

    A name with quotes or spaces is legal in SQLite but a nuisance afterwards,
    so better not to allow creating one.
    """
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", (name or "").strip()))


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def _db_status() -> dict[str, Any]:
    listing = tables()
    return {"tables": len(listing), "rows": sum(t["rows"] for t in listing),
            "kb": memory.stats()["dbBytes"] / 1024}


class _Gate(BaseHTTPMiddleware):
    """Lets /login and NiceGUI's static files through; the rest needs the password.

    Middleware and not a FastAPI dependency because NiceGUI's WebSocket does not
    go through dependencies.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith(PREFIX) or path.startswith(f"{PREFIX}/_nicegui"):
            return await call_next(request)
        if path.rstrip("/") == f"{PREFIX}/login":
            return await call_next(request)
        if not nicegui_app.storage.user.get("authed"):
            return RedirectResponse(f"{PREFIX}/login")
        return await call_next(request)


def build(mount_path: str = "/admin") -> None:
    """Mount the panel inside the FastAPI app that main.py hands over."""
    global PREFIX
    PREFIX = mount_path.rstrip("/")
    nicegui_app.add_middleware(_Gate)

    @ui.page("/login")
    def login() -> None:
        ui.add_head_html(f"<style>{CSS}</style>")
        ui.dark_mode().enable()

        def attempt() -> None:
            # compare_digest so the password does not leak through how long the
            # comparison takes to fail.
            if secrets.compare_digest(key.value or "", PASSWORD):
                nicegui_app.storage.user["authed"] = True
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
                    ui.label("Bonsai · database").style(
                        f"color:{C['fg']};font-weight:600")
                ui.label("Admin access").style(
                    f"color:{C['faint']};font-size:13px;padding-bottom:14px")
                key = ui.input(label="Password", password=True).props(
                    "outlined dense autofocus").classes("w-full")
                key.on("keydown.enter", attempt)
                error = ui.label("Wrong password").style(
                    f"color:{C['danger']};font-size:13px;padding-top:8px")
                error.set_visibility(False)
                with ui.row().classes("w-full pt-4"):
                    button("Sign in", attempt, kind="primary").classes("w-full")

    @ui.page("/")
    def page() -> None:
        ui.add_head_html(f"<style>{CSS}</style>")
        ui.query("body").style(f"background:{C['bg']}")
        ui.dark_mode().enable()

        p = Panel()
        status = _db_status()

        def logout() -> None:
            nicegui_app.storage.user.clear()
            ui.navigate.to("/login")

        # Header
        with ui.row().classes("w-full items-center gap-4 px-6 py-3").style(
            f"background:{C['surface']};border-bottom:1px solid {C['line']}"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.icon("storage").style(f"color:{C['accent']};font-size:20px")
                ui.label("Bonsai · database").style(
                    f"color:{C['fg']};font-weight:600;letter-spacing:-.01em")
            ui.space()
            for value, label in (
                (f"{status['tables']}", "tables"),
                (f"{status['rows']:,}", "rows"),
                (f"{status['kb']:.0f} KB", "size"),
            ):
                with ui.column().classes("items-end gap-0"):
                    ui.label(value).style(
                        f"color:{C['fg']};font-size:14px;font-weight:600;"
                        "font-variant-numeric:tabular-nums;line-height:1.2")
                    ui.label(label).style(
                        f"color:{C['faint']};font-size:10px;letter-spacing:.07em;"
                        "text-transform:uppercase")
            ui.element("div").style(f"width:1px;height:26px;background:{C['line']}")
            ui.link("Try it", "/provar").style(
                f"color:{C['dim']};font-size:13px;text-decoration:none").classes(
                "cursor-pointer hover:underline")
            ui.button(icon="logout", on_click=logout).props(
                "flat dense round size=sm color=grey-7").classes("cursor-pointer"
                ).tooltip("Sign out")

        # Body
        with ui.row().classes("body-row w-full gap-0 items-stretch").style(
            "min-height:calc(100vh - 57px)"
        ):
            with ui.column().classes("sidebar gap-0 p-3").style(
                f"width:250px;flex:none;border-right:1px solid {C['line']};"
                f"background:{C['surface']}"
            ):
                p.sidebar_container = ui.column().classes("w-full gap-1")
            p.main_container = ui.column().classes("main gap-0").style(
                "flex:1;min-width:0")

        p.draw_sidebar()
        # Open the first table: normally there is only one.
        first = tables()
        if first:
            p.open(first[0]["name"])
        else:
            p.draw_main()
