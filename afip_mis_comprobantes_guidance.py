# -*- coding: utf-8 -*-
"""
AFIP / ARCA - Mis Comprobantes (Emitidos y Recibidos)
-----------------------------------------------------
- Logs into AFIP with CUIT + clave
- Enters **MIS COMPROBANTES** service
- Selects CUIT objetivo
- Clicks on "Comprobantes Emitidos"
- Fills mandatory date range field (max 365 days)
- Clicks "Buscar" to search
- Exports results to CSV (Emitidos)
- Returns to Menu Principal
- Clicks on "Comprobantes Recibidos"
- Fills same date range
- Clicks "Buscar" to search
- Exports results to CSV (Recibidos)
- Downloads both CSV files

UI: Tkinter (login CUIT, clave, CUIT target, date range + Start button)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from playwright.async_api import async_playwright, TimeoutError, Download


logger = logging.getLogger(__name__)

def sanitize_secrets(message: str, clave: str) -> str:
    """Oculta SOLO la clave. Los CUITs son públicos y necesarios para debugging."""
    if clave and len(str(clave)) > 0:
        message = message.replace(str(clave), "****")
    return message

# ---------------- Config ---------------- #

LOGIN_URL = "https://auth.afip.gob.ar/contribuyente_/login.xhtml"

# Selectors for the MIS COMPROBANTES tile in the AFIP "portal" after login
MC_TILE_SELECTORS = [
    "a[title='mcmp']",
    "h3.roboto-font.bold.h5:has-text('MIS COMPROBANTES')",
    "a:has(h3:has-text('MIS COMPROBANTES'))",
]

# Selector for "Comprobantes Emitidos" button
EMITIDOS_SELECTOR = "a[href='comprobantesEmitidos.do'], a#btnEmitidos"

# Selector for "Comprobantes Recibidos" button
RECIBIDOS_SELECTOR = "a[href='comprobantesRecibidos.do'], a#btnRecibidos"

# Selector for "Menu Principal" breadcrumb link
MENU_PRINCIPAL_SELECTOR = "a[href='menuPrincipal.do']"

# Date field selector
DATE_FIELD_SELECTOR = "#fechaEmision"

# Buscar button selector
BUSCAR_BTN_SELECTOR = "#buscarComprobantes, button#buscarComprobantes"

# CSV export button selector
CSV_EXPORT_SELECTOR = "button:has-text('CSV'), .dt-buttons button:has-text('CSV')"

# Files / folders
SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
OUTPUT_DIR = Path.home() / "Downloads" / SCRIPT_PATH.stem
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- Helpers ---------------- #

DATE_FMT = "%d/%m/%Y"
CUIT_RE = re.compile(r"^\d{11}$")

def validar_cuit(cuit: str) -> bool:
    """Valida CUIT argentino incluyendo dígito verificador."""
    if not CUIT_RE.match(cuit):
        return False

    coef = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    digitos = [int(d) for d in cuit[:10]]
    suma = sum(d * c for d, c in zip(digitos, coef))

    verificador = 11 - (suma % 11)
    if verificador == 11:
        verificador = 0
    elif verificador == 10:
        verificador = 9

    return verificador == int(cuit[10])

def validar_rango_fecha(fecha_desde: str, fecha_hasta: str) -> Tuple[bool, str]:
    """
    Valida que las fechas estén en formato correcto y que el rango no exceda 365 días.
    Retorna (es_valido, mensaje_error)
    """
    try:
        desde = datetime.strptime(fecha_desde, DATE_FMT)
        hasta = datetime.strptime(fecha_hasta, DATE_FMT)

        if desde > hasta:
            return False, "La fecha 'desde' no puede ser posterior a la fecha 'hasta'"

        diferencia = (hasta - desde).days
        if diferencia > 365:
            return False, f"El rango de fechas excede el máximo permitido de 365 días (actual: {diferencia} días)"

        return True, ""
    except ValueError:
        return False, "Formato de fecha inválido. Use dd/mm/yyyy"

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- Scraper Core ---------------- #

async def _apply_viewport(page):
    try:
        await page.set_viewport_size({"width": 1920, "height": 1080})
    except Exception:
        pass

async def _afip_login(context, cuit_login: str, clave: str, on_log=print):
    page = await context.new_page()
    await _apply_viewport(page)
    on_log("Navegando al login AFIP...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(1)

    # CUIT + Siguiente
    on_log("Ingresando CUIT...")
    usr = page.locator("#F1\\:username")
    await usr.wait_for(state="visible", timeout=25000)
    await usr.click()
    await usr.fill(cuit_login)

    on_log("Click en Siguiente...")
    await page.locator("#F1\\:btnSiguiente").click()
    await page.wait_for_load_state("networkidle")

    # Clave + Ingresar
    on_log("Ingresando clave...")
    pwd = page.locator("#F1\\:password")
    await pwd.wait_for(state="visible", timeout=20000)
    await pwd.click()
    await pwd.fill(clave)
    await page.locator("#F1\\:btnIngresar").click()

    await page.wait_for_load_state("networkidle", timeout=45000)
    await asyncio.sleep(2)
    on_log("Login OK.")
    return page

async def _open_mis_comprobantes(context, portal_page, on_log=print):
    """From the AFIP portal home, open 'MIS COMPROBANTES' service (new tab or same tab)."""
    on_log("Buscando servicio MIS COMPROBANTES...")
    # Sometimes the tile is below a 'Ver todos' link
    try:
        for sel in MC_TILE_SELECTORS:
            if await portal_page.locator(sel).first.count():
                break
        else:
            ver_todos = portal_page.locator("a:has-text('Ver todos'), a[href*='mis-servicios']")
            if await ver_todos.count():
                await ver_todos.first.click()
                await portal_page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)
    except Exception:
        pass

    # Locate the tile/link
    link = None
    for sel in MC_TILE_SELECTORS:
        candidate = portal_page.locator(sel).first
        try:
            await candidate.wait_for(state="visible", timeout=4000)
            link = candidate
            break
        except TimeoutError:
            continue
    if link is None:
        # Search by heading text as a fallback
        link = portal_page.locator("a:has(h3:has-text('MIS COMPROBANTES'))").first

    on_log("Abriendo MIS COMPROBANTES...")
    new_page = None
    try:
        async with context.expect_page(timeout=8000) as new_page_info:
            await link.click()
        new_page = await new_page_info.value
    except TimeoutError:
        on_log("El servicio se abrió en la misma pestaña.")
        new_page = portal_page

    await new_page.wait_for_load_state("domcontentloaded")
    await new_page.wait_for_load_state("networkidle")
    await _apply_viewport(new_page)
    await asyncio.sleep(2)
    return new_page

async def _select_cuit_mc(page, cuit_target: str, on_log=print):
    """Select CUIT inside 'Mis Comprobantes' landing page."""
    try:
        # Look for the CUIT selection panel with the target CUIT
        on_log(f"Buscando CUIT objetivo: {cuit_target}...")

        # Wait for the selection page to load
        await page.wait_for_selector("form[name='seleccionaEmpresaForm']", timeout=10000)

        # Find the link/button that contains the target CUIT
        # The HTML shows: <a class="panel panel-default hoverazul" href="#" onclick="...">
        # With the CUIT in format XX-XXXXXXXX-X
        cuit_formatted = f"{cuit_target[:2]}-{cuit_target[2:10]}-{cuit_target[10]}"

        # Try to find by CUIT text
        selector = f"a.panel:has-text('{cuit_formatted}'), a.panel:has-text('{cuit_target}')"
        link = page.locator(selector).first

        if await link.count():
            on_log(f"Seleccionando CUIT: {cuit_formatted}...")
            await link.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)
        else:
            on_log("No se encontró selector de CUIT múltiples. Continuando...")
    except TimeoutError:
        on_log("Selector de CUIT no encontrado; se asume CUIT por defecto.")
    except Exception as e:
        on_log(f"Error al seleccionar CUIT: {e}")

async def _click_comprobantes_emitidos(page, on_log=print):
    """Click on 'Comprobantes Emitidos' button."""
    on_log("Navegando a Comprobantes Emitidos...")

    # Wait for the main menu to load
    await page.wait_for_selector("a[href='comprobantesEmitidos.do']", timeout=15000)

    # Click on "Emitidos"
    emitidos_btn = page.locator(EMITIDOS_SELECTOR).first
    await emitidos_btn.wait_for(state="visible", timeout=10000)
    await emitidos_btn.click()

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(2)
    on_log("✓ Página de Comprobantes Emitidos cargada")

async def _fill_date_field(page, fecha_desde: str, fecha_hasta: str, on_log=print):
    """
    Fill the date field with format 'dd/mm/yyyy - dd/mm/yyyy'.
    According to the user: "Para tipear la fecha en el campo resulta raro, no veo nada cambiar en el .html"
    This suggests we need to directly type into the input field.
    """
    on_log(f"Completando campo de fecha: {fecha_desde} - {fecha_hasta}...")

    # The date field format is: "dd/mm/yyyy - dd/mm/yyyy"
    date_string = f"{fecha_desde} - {fecha_hasta}"

    # Wait for the date field
    date_field = page.locator(DATE_FIELD_SELECTOR)
    await date_field.wait_for(state="visible", timeout=15000)

    # Click to focus
    await date_field.click()
    await asyncio.sleep(0.5)

    # Clear any existing value
    await date_field.fill("")
    await asyncio.sleep(0.3)

    # Type the date range
    await date_field.type(date_string, delay=100)  # Slow typing to ensure it registers
    await asyncio.sleep(0.5)

    # Verify the value was set
    value = await date_field.input_value()
    on_log(f"Valor del campo de fecha: '{value}'")

    if not value or value.strip() == "":
        on_log("⚠ El campo de fecha parece estar vacío. Intentando método alternativo...")
        # Try using JavaScript to set the value
        await page.evaluate(f"""
            const field = document.querySelector('{DATE_FIELD_SELECTOR}');
            if (field) {{
                field.value = '{date_string}';
                field.dispatchEvent(new Event('input', {{ bubbles: true }}));
                field.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        """)
        await asyncio.sleep(0.5)

    on_log("✓ Campo de fecha completado")

async def _click_buscar(page, on_log=print):
    """Click the 'Buscar' button and wait for results."""
    on_log("Haciendo click en Buscar...")

    buscar_btn = page.locator(BUSCAR_BTN_SELECTOR).first
    await buscar_btn.wait_for(state="visible", timeout=10000)
    await buscar_btn.scroll_into_view_if_needed()
    await buscar_btn.click()

    # Wait for the search to complete
    # The results appear in the "Resultados" tab
    on_log("Esperando resultados...")

    # Wait for the results tab to become active
    try:
        await page.wait_for_selector("#tabResultados.active", timeout=30000)
    except TimeoutError:
        on_log("⚠ Tab de resultados no se activó automáticamente")

    # Wait for the DataTable to load
    await page.wait_for_selector("#tablaDataTables, table.dataTable", timeout=30000)
    await asyncio.sleep(2)

    on_log("✓ Resultados cargados")

async def _export_to_csv(page, cuit_target: str, fecha_desde: str, fecha_hasta: str, tipo: str, on_log=print):
    """Click the CSV export button and handle the download.

    Args:
        tipo: "EMITIDOS" o "RECIBIDOS"
    """
    on_log(f"Exportando {tipo} a CSV...")

    # Wait for the CSV button to be visible
    csv_btn = page.locator(CSV_EXPORT_SELECTOR).first
    await csv_btn.wait_for(state="visible", timeout=15000)
    await csv_btn.scroll_into_view_if_needed()

    # Set up download handler
    async with page.expect_download(timeout=30000) as download_info:
        await csv_btn.click()
        download = await download_info.value

    # Generate filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    desde_fmt = fecha_desde.replace("/", "")
    hasta_fmt = fecha_hasta.replace("/", "")
    filename = f"MC_{tipo}_{cuit_target}_{desde_fmt}_{hasta_fmt}_{timestamp}.csv"
    save_path = OUTPUT_DIR / filename

    # Save the download
    await download.save_as(save_path)
    on_log(f"✓ CSV {tipo} guardado: {save_path}")

    return str(save_path)

async def _click_menu_principal(page, on_log=print):
    """Navigate back to Menu Principal using the breadcrumb link."""
    on_log("Navegando a Menú Principal...")

    # Click on breadcrumb "Menu Principal"
    menu_btn = page.locator(MENU_PRINCIPAL_SELECTOR).first
    await menu_btn.wait_for(state="visible", timeout=10000)
    await menu_btn.click()

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(2)
    on_log("✓ Menú Principal cargado")

async def _click_comprobantes_recibidos(page, on_log=print):
    """Click on 'Comprobantes Recibidos' button."""
    on_log("Navegando a Comprobantes Recibidos...")

    # Wait for the main menu to load
    await page.wait_for_selector("a[href='comprobantesRecibidos.do']", timeout=15000)

    # Click on "Recibidos"
    recibidos_btn = page.locator(RECIBIDOS_SELECTOR).first
    await recibidos_btn.wait_for(state="visible", timeout=10000)
    await recibidos_btn.click()

    await page.wait_for_load_state("networkidle", timeout=20000)
    await asyncio.sleep(2)
    on_log("✓ Página de Comprobantes Recibidos cargada")

async def scrape_mis_comprobantes(
    cuit_login: str,
    clave: str,
    cuit_target: str,
    fecha_desde: str,
    fecha_hasta: str,
    on_log=print
):
    """Main scraper function for Mis Comprobantes."""
    # Validate date range
    es_valido, mensaje = validar_rango_fecha(fecha_desde, fecha_hasta)
    if not es_valido:
        on_log(f"❌ Error de validación: {mensaje}")
        raise ValueError(mensaje)

    on_log("Iniciando navegador...")
    temp_profile = Path(tempfile.gettempdir()) / f"studioai_mc_{uuid.uuid4().hex[:8]}"
    temp_profile.mkdir(parents=True, exist_ok=True)
    temp_dir = str(temp_profile)
    on_log(f"Usando profile temporal: {temp_dir}")

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=temp_dir,
                headless=False,
                accept_downloads=True
            )
            for p in context.pages:
                await _apply_viewport(p)

            # Login
            portal = await _afip_login(context, cuit_login, clave, on_log=on_log)

            # Open MIS COMPROBANTES
            mc_page = await _open_mis_comprobantes(context, portal, on_log=on_log)

            # Select CUIT target
            await _select_cuit_mc(mc_page, cuit_target, on_log=on_log)

            # ========== COMPROBANTES EMITIDOS ==========
            on_log("")
            on_log("=" * 50)
            on_log("PASO 1: COMPROBANTES EMITIDOS")
            on_log("=" * 50)

            # Click on "Comprobantes Emitidos"
            await _click_comprobantes_emitidos(mc_page, on_log=on_log)

            # Fill date field
            await _fill_date_field(mc_page, fecha_desde, fecha_hasta, on_log=on_log)

            # Click Buscar
            await _click_buscar(mc_page, on_log=on_log)

            # Export to CSV Emitidos
            csv_emitidos = await _export_to_csv(mc_page, cuit_target, fecha_desde, fecha_hasta, "EMITIDOS", on_log=on_log)

            # ========== COMPROBANTES RECIBIDOS ==========
            on_log("")
            on_log("=" * 50)
            on_log("PASO 2: COMPROBANTES RECIBIDOS")
            on_log("=" * 50)

            # Navigate back to Menu Principal
            await _click_menu_principal(mc_page, on_log=on_log)

            # Click on "Comprobantes Recibidos"
            await _click_comprobantes_recibidos(mc_page, on_log=on_log)

            # Fill date field (same dates)
            await _fill_date_field(mc_page, fecha_desde, fecha_hasta, on_log=on_log)

            # Click Buscar
            await _click_buscar(mc_page, on_log=on_log)

            # Export to CSV Recibidos
            csv_recibidos = await _export_to_csv(mc_page, cuit_target, fecha_desde, fecha_hasta, "RECIBIDOS", on_log=on_log)

            # ========== COMPLETADO ==========
            on_log("")
            on_log("=" * 50)
            on_log("✅ PROCESO COMPLETADO 🎉")
            on_log("=" * 50)
            on_log(f"CSV Emitidos:  {csv_emitidos}")
            on_log(f"CSV Recibidos: {csv_recibidos}")

            return {
                "emitidos": csv_emitidos,
                "recibidos": csv_recibidos
            }

    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Perfil temporal eliminado: {temp_dir}")
        except Exception as e:
            logger.warning(f"No se pudo eliminar perfil temporal: {e}")

# ---------------- Tkinter GUI ---------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AFIP - MIS COMPROBANTES (Emitidos + Recibidos)")
        self.resizable(False, False)

        frm = ttk.Frame(self, padding="10")
        frm.grid(row=0, column=0, sticky="nsew")

        # CUIT login
        ttk.Label(frm, text="CUIT (login) *").grid(row=0, column=0, sticky="w")
        self.e_cuit = ttk.Entry(frm, width=22)
        self.e_cuit.grid(row=0, column=1, padx=8, pady=6)

        # Clave
        ttk.Label(frm, text="Clave *").grid(row=1, column=0, sticky="w")
        self.e_clave = ttk.Entry(frm, width=22, show="*")
        self.e_clave.grid(row=1, column=1, padx=8, pady=6)

        # CUIT target
        ttk.Label(frm, text="CUIT target *").grid(row=2, column=0, sticky="w")
        self.e_cuit_target = ttk.Entry(frm, width=22)
        self.e_cuit_target.grid(row=2, column=1, padx=8, pady=6)

        # Fecha desde
        ttk.Label(frm, text="Fecha desde (dd/mm/yyyy) *").grid(row=3, column=0, sticky="w")
        self.e_fecha_desde = ttk.Entry(frm, width=22)
        self.e_fecha_desde.grid(row=3, column=1, padx=8, pady=6)
        # Set default to 30 days ago
        fecha_desde_default = (datetime.now() - timedelta(days=30)).strftime(DATE_FMT)
        self.e_fecha_desde.insert(0, fecha_desde_default)

        # Fecha hasta
        ttk.Label(frm, text="Fecha hasta (dd/mm/yyyy) *").grid(row=4, column=0, sticky="w")
        self.e_fecha_hasta = ttk.Entry(frm, width=22)
        self.e_fecha_hasta.grid(row=4, column=1, padx=8, pady=6)
        # Set default to today
        fecha_hasta_default = datetime.now().strftime(DATE_FMT)
        self.e_fecha_hasta.insert(0, fecha_hasta_default)

        # Info label
        info_label = ttk.Label(frm, text="Rango máximo: 365 días", foreground="gray")
        info_label.grid(row=5, column=0, columnspan=2, pady=(0, 6))

        # Start button
        self.btn = ttk.Button(frm, text="Iniciar", command=self.on_start)
        self.btn.grid(row=6, column=0, columnspan=2, sticky="ew", padx=8, pady=(10, 6))

        # Log area
        ttk.Label(frm, text="Log:").grid(row=7, column=0, columnspan=2, sticky="w")
        self.log = tk.Text(frm, height=14, width=60, state="disabled", wrap="word")
        self.log.grid(row=8, column=0, columnspan=2, padx=8, pady=6)
        scroll = ttk.Scrollbar(frm, command=self.log.yview)
        scroll.grid(row=8, column=2, sticky="ns")
        self.log.config(yscrollcommand=scroll.set)

    def log_line(self, msg: str):
        self.log.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    def validate(self) -> Optional[Tuple[str, str, str, str, str]]:
        cuit = self.e_cuit.get().strip()
        clave = self.e_clave.get().strip()
        cuit_target = self.e_cuit_target.get().strip()
        fecha_desde = self.e_fecha_desde.get().strip()
        fecha_hasta = self.e_fecha_hasta.get().strip()

        if not validar_cuit(cuit):
            messagebox.showerror("Validación",
                                 "CUIT (login) inválido.\nDebe tener 11 dígitos y el dígito verificador correcto.")
            return None
        if not validar_cuit(cuit_target):
            messagebox.showerror("Validación",
                                 "CUIT target inválido.\nDebe tener 11 dígitos y el dígito verificador correcto.")
            return None
        if not clave:
            messagebox.showerror("Validación", "La clave no puede estar vacía.")
            return None

        # Validate dates
        es_valido, mensaje = validar_rango_fecha(fecha_desde, fecha_hasta)
        if not es_valido:
            messagebox.showerror("Validación de Fechas", mensaje)
            return None

        return cuit, clave, cuit_target, fecha_desde, fecha_hasta

    def on_start(self):
        data = self.validate()
        if not data:
            return

        self.btn.configure(state="disabled", text="Procesando...")
        self.log_line("✓ Validación OK. Arrancando...")

        cuit, clave, cuit_target, fecha_desde, fecha_hasta = data

        def worker():
            try:
                def _on_log(msg):
                    self.after(0, self.log_line, msg)

                result = asyncio.run(scrape_mis_comprobantes(
                    cuit, clave, cuit_target, fecha_desde, fecha_hasta, on_log=_on_log
                ))
                self.after(0, lambda: messagebox.showinfo(
                    "Listo",
                    f"Proceso finalizado.\n\n"
                    f"Archivos guardados:\n\n"
                    f"EMITIDOS:\n{result['emitidos']}\n\n"
                    f"RECIBIDOS:\n{result['recibidos']}"
                ))
            except Exception as e:
                err_msg = f"{e}"
                import traceback
                full_trace = traceback.format_exc()
                self.after(0, lambda m=err_msg: messagebox.showerror("Error", m))
                self.after(0, lambda t=full_trace: self.log_line(f"ERROR COMPLETO:\n{t}"))
            finally:
                self.after(0, lambda: self.btn.configure(state="normal", text="Iniciar"))

        threading.Thread(target=worker, daemon=True).start()

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
