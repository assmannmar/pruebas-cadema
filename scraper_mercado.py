"""
scraper_mercado.py — Foto de mercado Argenprop → CSV + Google Sheet (para Looker)
=================================================================================

Recorre Campana / Zárate / Exaltación de la Cruz × Casas / Deptos en Argenprop,
lee todas las tarjetas de resultados y, para los avisos nuevos, la ficha
(sup. terreno, localidad, estado…). Guarda:

  datos/foto_AAAA-MM.csv    la foto del mes
  datos/base.csv            todas las fotos juntas (una fila por aviso y foto)
  datos/detalle_cache.csv   fichas ya visitadas (cada aviso se visita una sola vez)

y, si configurás SHEET_URL, sube la foto a la hoja "Base" del Google Sheet que lee Looker.

Usa un navegador real (Chromium vía Playwright) porque Argenprop tiene un
filtro anti-bots (AWS WAF) que bloquea pedidos "pelados" (requests, Apps Script).

Instalación (una vez):
    pip install playwright requests
    python -m playwright install chromium

Uso:
    python scraper_mercado.py                 # foto completa
    python scraper_mercado.py --prueba        # 1 página por búsqueda, para probar
    python scraper_mercado.py --sin-detalle   # sólo listados (más rápido)
    python scraper_mercado.py --ver           # muestra el navegador (si el headless es bloqueado)
    python scraper_mercado.py --solo-subir datos/foto_2026-10.csv   # re-subir una foto al Sheet
"""

from __future__ import annotations

import argparse
import csv
import html as htmllib
import json
import math
import random
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ───────────────────────────── CONFIGURACIÓN ─────────────────────────────

BASE_URL = "https://www.argenprop.com"
FUENTE = "Argenprop"

BUSQUEDAS = [
    # (ciudad,                 tipo,           ruta)
    ("Campana",               "Casa",         "/casas/venta/partido-de-campana"),
    ("Campana",               "Departamento", "/departamentos/venta/partido-de-campana"),
    ("Zárate",                "Casa",         "/casas/venta/partido-de-zarate"),
    ("Zárate",                "Departamento", "/departamentos/venta/partido-de-zarate"),
    ("Exaltación de la Cruz", "Casa",         "/casas/venta/partido-de-exaltacion-de-la-cruz"),
    ("Exaltación de la Cruz", "Departamento", "/departamentos/venta/partido-de-exaltacion-de-la-cruz"),
    # ("Campana",             "PH",           "/ph/venta/partido-de-campana"),
    # ("Zárate",              "PH",           "/ph/venta/partido-de-zarate"),
]

# URL de la Web App de Apps Script (receptor_sheet.gs) y su clave. Vacío = no sube.
SHEET_URL = ""
SHEET_TOKEN = "cambiar-esta-clave"

PAUSA_SEG = (1.2, 2.5)        # pausa aleatoria entre pedidos
AVISOS_POR_PAGINA = 20
CARPETA = Path(__file__).resolve().parent / "datos"

COLS_BASE = [
    "foto", "fecha_foto", "fuente", "id_aviso", "ciudad", "tipo",
    "localidad", "zona", "barrio", "direccion", "titulo",
    "moneda", "precio", "precio_usd", "precio_oculto", "usd_m2_cub",
    "sup_cubierta", "sup_terreno", "sup_total",
    "ambientes", "dormitorios", "banos", "cocheras", "antiguedad",
    "estado", "disposicion", "orientacion", "reservado",
    "inmobiliaria", "id_anunciante", "id_localidad", "id_barrio",
    "pagina", "posicion", "url",
]
COLS_DETALLE = [
    "id_aviso", "fecha_visita", "localidad", "barrio_detalle",
    "sup_cubierta", "sup_terreno", "sup_total", "sup_descubierta",
    "estado", "disposicion", "orientacion", "expensas", "apto_credito", "ok",
]

# ───────────────────────────── PARSERS ─────────────────────────────


def limpiar(s) -> str:
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def num(s):
    """'71,46 m² cubie.' → 71.46 · '1.033 m2' → 1033 · 'USD 280.000' → 280000"""
    if s is None or s == "":
        return ""
    m = re.search(r"\d[\d.,]*", str(s))
    if not m:
        return ""
    t = re.sub(r"\.(?=\d{3}(\D|$))", "", m.group(0)).replace(",", ".")
    try:
        n = float(t)
    except ValueError:
        return ""
    return int(n) if n.is_integer() else n


def atributos(tag: str) -> dict:
    return {k.lower(): htmllib.unescape(v) for k, v in re.findall(r'([\w-]+)="([^"]*)"', tag)}


def parse_listado(html: str) -> dict:
    out = {"total": None, "avisos": []}
    mt = re.search(r"<title>\s*([\d.]+)\s", html, re.I)
    if mt:
        out["total"] = int(mt.group(1).replace(".", ""))

    # cortar "avisos sugeridos" (la clase también aparece en el CSS inline)
    mc = re.search(r'class="[^"]*listing-container--suggested', html)
    if mc:
        html = html[: mc.start()]

    for t in re.split(r'<div class="listing__item[\s"]', html)[1:]:
        ma = re.search(r'<a\s[^>]*data-item-card="\d+"[^>]*>', t)
        if not ma:
            continue
        at = atributos(ma.group(0))

        ul = re.search(r'class="card__main-features"[\s\S]*?</ul>', t)
        feats = {k: limpiar(v) for k, v in re.findall(
            r'basico1-icon-([a-z_]+)"[^>]*></i>\s*<span>([\s\S]*?)</span>', ul.group(0) if ul else "")}

        def grab(rx):
            m = re.search(rx, t)
            return m.group(1) if m else ""

        direccion_full = limpiar(grab(r'class="card__address"[^>]*>([\s\S]*?)</p>'))
        precio_html = grab(r'class="card__price"[^>]*>([\s\S]*?)</p>')
        titulo = limpiar(grab(r'class="card__title"[^>]*>([\s\S]*?)</h2>'))
        inmo = limpiar(grab(r'class="card__agent-name"[^>]*>([\s\S]*?)</p>'))

        monto = num(at.get("montooperacion"))
        mon_txt = limpiar((re.search(r'card__currency"[^>]*>([^<]*)<', precio_html) or [None, ""])[1])
        idm = at.get("idmoneda")
        moneda = ("USD" if idm == "2" or re.search(r"USD|U\$S", mon_txt, re.I)
                  else "ARS" if idm == "1" or mon_txt == "$" else mon_txt)
        sin_precio = ("card__noprice" in precio_html) or not monto

        # "Moreno 300, Centro - Campana" · "Alem 500, Piso 4, Zárate"
        partes = [limpiar(p) for p in direccion_full.split(",")]
        barrio = partes.pop().split(" - ")[0].strip() if len(partes) > 1 else ""
        direccion = ", ".join(partes)

        amb = feats.get("cantidad_ambientes", "")
        ant = feats.get("antiguedad", "")
        out["avisos"].append({
            "id": at.get("idaviso") or at.get("data-item-card"),
            "url": BASE_URL + at.get("href", ""),
            "titulo": titulo,
            "direccion": direccion,
            "barrio": barrio,
            "moneda": moneda,
            "precio": monto or "",
            "precio_oculto": sin_precio,
            "sup_cubierta": num(feats.get("superficie_cubierta")),
            "ambientes": 1 if re.search("mono", amb, re.I) else (num(amb) or num(at.get("ambientes"))),
            "dormitorios": num(feats.get("cantidad_dormitorios")) or num(at.get("dormitorios")),
            "banos": num(feats.get("cantidad_banos")),
            "cocheras": num(feats.get("ambiente_cochera")),
            "antiguedad": 0 if re.search("estrenar", ant, re.I) else num(ant),
            "estado": feats.get("estado_propiedad", ""),
            "disposicion": feats.get("disposicion", ""),
            "reservado": bool(re.search("reservad", titulo, re.I)),
            "inmobiliaria": inmo,
            "id_anunciante": at.get("idanunciante", ""),
            "id_localidad": at.get("idlocalidad", ""),
            "id_barrio": at.get("idbarrio", ""),
        })
    return out


def _json_str(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def parse_detalle(html: str) -> dict:
    kv = {limpiar(k).lower(): limpiar(v) for k, v in re.findall(
        r"<h3>\s*([^<:]+?)\s*:\s*<strong>\s*([\s\S]*?)\s*</strong>", html)}
    loc = re.search(r'"addressLocality"\s*:\s*"([^"]*)"', html)
    reg = re.search(r'"addressRegion"\s*:\s*"([^"]*)"', html)
    localidad = re.sub(r",\s*Argentina$", "", limpiar(_json_str(loc.group(1)))) if loc else ""
    return {
        "localidad": localidad,
        "barrio_detalle": limpiar(_json_str(reg.group(1))) if reg else "",
        "sup_cubierta": num(kv.get("sup. cubierta")),
        "sup_terreno": num(kv.get("sup. terreno")),
        "sup_total": num(kv.get("sup. total")),
        "sup_descubierta": num(kv.get("sup. descubierta")),
        "estado": kv.get("estado", ""),
        "disposicion": kv.get("disposición") or kv.get("disposicion", ""),
        "orientacion": kv.get("orientación") or kv.get("orientacion", ""),
        "expensas": num(kv.get("expensas")),
        "apto_credito": bool(re.search(r"apto cr[eé]dito", html, re.I)),
    }


def clasificar_zona(*textos) -> str:
    s = " ".join(str(x) for x in textos).lower()
    return ("Country / B. cerrado"
            if re.search(r"countr|barrios? cerrado|barrio privado|club de campo|chacras|village|la reserva", s)
            else "Ciudad")

# ───────────────────────────── NAVEGADOR ─────────────────────────────


class Navegador:
    """Chromium real: resuelve solo el desafío anti-bots y reutiliza la cookie para pedidos rápidos."""

    def __init__(self, visible=False):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=not visible, args=["--disable-blink-features=AutomationControlled"])
        self.ctx = self.browser.new_context(
            locale="es-AR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900})
        self.page = self.ctx.new_page()
        self.page.route(re.compile(r"\.(png|jpe?g|webp|gif|svg|woff2?)(\?|$)"), lambda r: r.abort())
        self._resolver_desafio(BASE_URL + BUSQUEDAS[0][2])

    def _resolver_desafio(self, url):
        """Navega con el navegador completo (corre el JS del desafío y obtiene la cookie aws-waf-token)."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            self.page.wait_for_selector(".listing__item, .titlebar, .property-description", timeout=30_000)
        except Exception:
            pass
        return self.page.content()

    def get(self, url: str) -> str | None:
        for intento in range(4):
            try:
                r = self.ctx.request.get(url, timeout=45_000)
                body = r.text()
                if r.status == 200 and len(body) > 5000:
                    return body
                if r.status == 404:
                    return None
                # 202/403/429 → desafío anti-bots: resolverlo con el navegador y reintentar
                print(f"    · desafío anti-bots (HTTP {r.status}), resolviendo…", flush=True)
                html = self._resolver_desafio(url)
                if "data-item-card" in html or "titlebar" in html:
                    return html
            except Exception as e:  # red, timeout
                print(f"    · error {type(e).__name__}: {e}", flush=True)
            time.sleep(10 * (intento + 1))
        raise RuntimeError(f"No pude leer {url} (bloqueado). Probá con --ver o más tarde.")

    def cerrar(self):
        try:
            self.browser.close()
            self._pw.stop()
        except Exception:
            pass


def pausa():
    time.sleep(random.uniform(*PAUSA_SEG))

# ───────────────────────────── CSV ─────────────────────────────


def leer_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def escribir_csv(path: Path, filas: list[dict], cols: list[str], agregar=False):
    nuevo = not path.exists() or not agregar
    with path.open("a" if agregar else "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerows(filas)

# ───────────────────────────── FLUJO ─────────────────────────────


def sacar_foto(prueba=False, con_detalle=True, visible=False) -> Path:
    CARPETA.mkdir(exist_ok=True)
    hoy = date.today()
    foto, fecha = hoy.strftime("%Y-%m"), hoy.isoformat()
    print(f"\n=== Foto {foto} ({FUENTE}) ===")

    nav = Navegador(visible=visible)
    filas, vistos = [], set()
    try:
        # 1) Listados
        for ciudad, tipo, ruta in BUSQUEDAS:
            pagina, total_pag = 1, 1
            while pagina <= total_pag:
                url = BASE_URL + ruta + (f"?pagina-{pagina}" if pagina > 1 else "")
                html = nav.get(url) or ""
                res = parse_listado(html)
                if pagina == 1:
                    total_pag = max(1, math.ceil((res["total"] or 0) / AVISOS_POR_PAGINA))
                    if prueba:
                        total_pag = 1
                    print(f"{ciudad} / {tipo}: {res['total']} avisos, {total_pag} páginas")
                if not res["avisos"]:
                    break
                for i, a in enumerate(res["avisos"], 1):
                    if a["id"] in vistos:
                        continue
                    vistos.add(a["id"])
                    filas.append({**a, "id_aviso": a["id"], "ciudad": ciudad, "tipo": tipo,
                                  "pagina": pagina, "posicion": i})
                print(f"  pág. {pagina}/{total_pag} · acumulado {len(filas)}", end="\r", flush=True)
                pagina += 1
                pausa()
            print()

        # 2) Fichas de avisos nuevos (caché)
        cache_path = CARPETA / "detalle_cache.csv"
        cache = {r["id_aviso"]: r for r in leer_csv(cache_path)}
        if con_detalle:
            pendientes = [f for f in filas if f["id_aviso"] not in cache]
            if prueba:
                pendientes = pendientes[:5]
            print(f"Fichas nuevas a visitar: {len(pendientes)} (ya en caché: {len(filas) - len(pendientes)})")
            nuevos = []
            for n, f in enumerate(pendientes, 1):
                html = nav.get(f["url"])
                d = parse_detalle(html) if html else {}
                reg = {"id_aviso": f["id_aviso"], "fecha_visita": fecha, "ok": bool(html), **d}
                cache[f["id_aviso"]] = reg
                nuevos.append(reg)
                if len(nuevos) >= 25:  # guardar seguido: si se corta, no se pierde lo hecho
                    escribir_csv(cache_path, nuevos, COLS_DETALLE, agregar=True)
                    nuevos = []
                print(f"  ficha {n}/{len(pendientes)}", end="\r", flush=True)
                pausa()
            escribir_csv(cache_path, nuevos, COLS_DETALLE, agregar=True)
            print()
    finally:
        nav.cerrar()

    # 3) Armar filas finales
    salida = []
    for f in filas:
        d = cache.get(f["id_aviso"], {})
        sup_cub = f["sup_cubierta"] or num(d.get("sup_cubierta"))
        precio_usd = f["precio"] if f["moneda"] == "USD" and f["precio"] else ""
        localidad = d.get("localidad", "")
        salida.append({
            **f,
            "foto": foto, "fecha_foto": fecha, "fuente": FUENTE,
            "localidad": localidad,
            "zona": clasificar_zona(localidad, f["barrio"], f["url"]),
            "sup_cubierta": sup_cub,
            "sup_terreno": d.get("sup_terreno", ""),
            "sup_total": d.get("sup_total", ""),
            "estado": f["estado"] or d.get("estado", ""),
            "disposicion": f["disposicion"] or d.get("disposicion", ""),
            "orientacion": d.get("orientacion", ""),
            "precio_usd": precio_usd,
            "usd_m2_cub": round(precio_usd / float(sup_cub)) if precio_usd and sup_cub else "",
        })

    path_foto = CARPETA / f"foto_{foto}{'_prueba' if prueba else ''}.csv"
    escribir_csv(path_foto, salida, COLS_BASE)
    if not prueba:
        base_path = CARPETA / "base.csv"
        previas = [r for r in leer_csv(base_path) if r.get("foto") != foto]  # re-correr el mes reemplaza
        escribir_csv(base_path, previas + salida, COLS_BASE)
    print(f"Listo: {len(salida)} avisos → {path_foto}")
    return path_foto


def subir_a_sheet(path_foto: Path):
    if not SHEET_URL:
        print("SHEET_URL vacío: no se sube al Google Sheet (el CSV quedó en la carpeta datos/).")
        return
    import requests
    filas = leer_csv(path_foto)
    if not filas:
        print("CSV vacío, nada para subir.")
        return
    foto = filas[0]["foto"]
    lote = 500
    for i in range(0, len(filas), lote):
        cuerpo = {
            "token": SHEET_TOKEN, "foto": foto, "columnas": COLS_BASE,
            "filas": [[r.get(c, "") for c in COLS_BASE] for r in filas[i:i + lote]],
            "reemplazar": i == 0,  # el primer lote borra lo que hubiera de esa foto
        }
        r = requests.post(SHEET_URL, data=json.dumps(cuerpo), timeout=120,
                          headers={"Content-Type": "text/plain"})
        r.raise_for_status()
        resp = r.json()
        if not resp.get("ok"):
            raise RuntimeError(f"El Sheet respondió error: {resp}")
        print(f"  subidas {min(i + lote, len(filas))}/{len(filas)}")
    print(f"Foto {foto} cargada en el Google Sheet.")


def main():
    ap = argparse.ArgumentParser(description="Foto de mercado Argenprop")
    ap.add_argument("--prueba", action="store_true", help="1 página por búsqueda y 5 fichas")
    ap.add_argument("--sin-detalle", action="store_true", help="no visitar fichas")
    ap.add_argument("--ver", action="store_true", help="mostrar el navegador")
    ap.add_argument("--solo-subir", metavar="CSV", help="sólo subir un CSV ya generado al Sheet")
    a = ap.parse_args()

    if a.solo_subir:
        subir_a_sheet(Path(a.solo_subir))
        return
    t0 = datetime.now()
    path = sacar_foto(prueba=a.prueba, con_detalle=not a.sin_detalle, visible=a.ver)
    if not a.prueba:
        subir_a_sheet(path)
    print(f"Tiempo total: {datetime.now() - t0}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCortado. Lo ya visitado quedó en datos/detalle_cache.csv; al re-correr sigue desde ahí.")