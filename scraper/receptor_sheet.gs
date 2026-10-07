/**
 * Receptor para scraper_mercado.py → escribe la foto en la hoja "Base".
 *
 * Instalación (una vez), en el Google Sheet que lee Looker:
 *  1. Extensiones → Apps Script → borrar todo y pegar este archivo.
 *  2. Cambiar TOKEN por una clave cualquiera (la misma que SHEET_TOKEN en el .py).
 *  3. Implementar → Nueva implementación → tipo "Aplicación web"
 *       Ejecutar como: Yo   ·   Quién tiene acceso: Cualquier usuario
 *  4. Copiar la URL que termina en /exec y pegarla en SHEET_URL del .py.
 *
 * Si después cambiás este código: Implementar → Gestionar implementaciones →
 * editar (lápiz) → Versión: nueva → Implementar (la URL no cambia).
 */

const TOKEN = 'scraper-argenprop';
const HOJA = 'Base';

function doPost(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.token !== TOKEN) return json_({ ok: false, error: 'token inválido' });

    const ss = SpreadsheetApp.getActive();
    let sh = ss.getSheetByName(HOJA);
    if (!sh) sh = ss.insertSheet(HOJA);
    const cols = body.columnas;

    // Si la hoja tiene otro encabezado (versión anterior), se guarda aparte y se arranca limpia
    if (sh.getLastRow() > 0) {
      const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0].map(String);
      if (head.slice(0, cols.length).join('|') !== cols.join('|')) {
        sh.setName(HOJA + '_anterior_' + Utilities.formatDate(new Date(), 'GMT-3', 'yyyyMMdd_HHmm'));
        sh = ss.insertSheet(HOJA);
      }
    }
    if (sh.getLastRow() === 0) {
      sh.getRange(1, 1, 1, cols.length).setValues([cols]).setFontWeight('bold');
      sh.setFrozenRows(1);
    }
    const colFoto = cols.indexOf('foto') + 1;   // "2026-10" como texto, no como fecha
    sh.getRange(1, colFoto, sh.getMaxRows(), 1).setNumberFormat('@');

    // Re-correr un mes reemplaza esa foto (no duplica)
    let borradas = 0;
    if (body.reemplazar && sh.getLastRow() > 1) {
      const fotos = sh.getRange(2, colFoto, sh.getLastRow() - 1, 1).getDisplayValues().map(r => r[0]);
      const resto = [];
      const todo = sh.getRange(2, 1, sh.getLastRow() - 1, cols.length).getValues();
      todo.forEach((r, i) => { if (fotos[i] !== body.foto) resto.push(r); else borradas++; });
      if (borradas) {
        sh.getRange(2, 1, todo.length, cols.length).clearContent();
        if (resto.length) sh.getRange(2, 1, resto.length, cols.length).setValues(resto);
      }
    }

    const filas = body.filas.map(r => r.map(convertir_));
    if (filas.length) {
      sh.getRange(sh.getLastRow() + 1, 1, filas.length, cols.length).setValues(filas);
    }
    return json_({ ok: true, agregadas: filas.length, borradas: borradas });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/** Números como números y True/False como booleanos, para que Looker los tome bien. */
function convertir_(v) {
  if (v === 'True' || v === 'true') return true;
  if (v === 'False' || v === 'false') return false;
  if (typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v) && v.length < 15) return Number(v);
  return v;
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

/** Para probar que la URL anda: abrila en el navegador. */
function doGet() {
  return json_({ ok: true, hoja: HOJA, filas: (SpreadsheetApp.getActive().getSheetByName(HOJA) || { getLastRow: () => 0 }).getLastRow() });
}
