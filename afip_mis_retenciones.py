# -*- coding: utf-8 -*-
"""
AFIP / ARCA - Mis Retenciones
------------------------------
- Logs into AFIP with CUIT + clave
- Enters **MIS RETENCIONES** service
- Selects CUIT objetivo (representado)
- Fills in tax type, operation type, and date range
- Clicks "Consultar" to search
- Exports results to CSV
- Handles popup and navigates to "Consultas exportadas"
- Waits for file to be ready and downloads it

UI: Tkinter (login CUIT, clave, CUIT target, tax type, dates + Start button)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, Dict, List

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

# Selector for the MIS RETENCIONES tile in the AFIP "portal" after login
MR_TILE_SELECTORS = [
    "a[title='mis_retenciones']",
    "h3.roboto-font.bold.h5:has-text('MIS RETENCIONES')",
    "a:has(h3:has-text('MIS RETENCIONES'))",
]

# Files / folders
SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent
OUTPUT_DIR = Path.home() / "Downloads" / SCRIPT_PATH.stem
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- Tax Types Configuration ---------------- #

@dataclass
class TaxTypeConfig:
    """Configuración para cada tipo de impuesto."""
    code: str  # e.g., "IMP_217"
    name: str  # e.g., "217 - SICORE-IMPTO.A LAS GANANCIAS"
    category: str  # "Impositivas", "Seguridad Social", "Aduaneras", "Certificados SIRE"
    # Operation type options:
    # - "retencion": Solo Retención
    # - "percepcion": Solo Percepción
    # - "ambas": Retención y percepción (simultáneas)
    # - "ambas_separadas": Hacer 2 consultas (primero retención, luego percepción)
    # - "fecha_solo": Solo fecha (no hay campo tipo operación)
    operation_mode: str

# Mapping de todos los tipos de impuestos
TAX_TYPES = [
    # Impositivas
    TaxTypeConfig("IMP_172", "172 - IMPUESTO TRANSF DE INMUEBLES", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_216", "216 - SIRE - IVA", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_217", "217 - SICORE-IMPTO.A LAS GANANCIAS", "Impositivas", "ambas"),
    TaxTypeConfig("IMP_218", "218 - IMP.A LAS GAN.- BENEF.DEL EXT.", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_219", "219 - SICORE-IMPTO.S/ BS PERSONALES", "Impositivas", "ambas_separadas"),
    TaxTypeConfig("IMP_222", "222 - DONACIONES Y OTRO TIPO DE LIB.", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_466", "466 - SICORE-PREMIOS JUEGOS Y C.DEP", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_767", "767 - SICORE - RETENCIONES Y PERCEPC", "Impositivas", "ambas"),
    TaxTypeConfig("IMP_787", "787 - RET ART 79 LEY GCIAS INC A,ByC", "Impositivas", "retencion"),
    TaxTypeConfig("IMP_939", "939 - PERCEPCION IMPUESTO PAIS", "Impositivas", "percepcion"),
    # Seguridad Social
    TaxTypeConfig("SS_353", "353 - RETENCIONES CONTRIB.SEG.SOCIAL", "Seguridad Social", "ambas"),
    # Aduaneras
    TaxTypeConfig("ADU_217", "217 - SICORE-IMPTO.A LAS GANANCIAS", "Aduaneras", "fecha_solo"),
    TaxTypeConfig("ADU_767", "767 - SICORE - RETENCIONES Y PERCEPC", "Aduaneras", "fecha_solo"),
    # Certificados SIRE
    TaxTypeConfig("SIR_216", "216 - SIRE - IVA", "Certificados SIRE", "retencion"),
    TaxTypeConfig("SIR_218", "218 - IMP.A LAS GAN.- BENEF.DEL EXT.", "Certificados SIRE", "retencion"),
    TaxTypeConfig("SIR_353", "353 - RETENCIONES CONTRIB.SEG.SOCIAL", "Certificados SIRE", "ambas"),
]

# Create a lookup dict
TAX_TYPES_DICT = {tax.code: tax for tax in TAX_TYPES}

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
    Valida que las fechas estén en formato correcto.
    Retorna (es_valido, mensaje_error)
    """
    try:
        desde = datetime.strptime(fecha_desde, DATE_FMT)
        hasta = datetime.strptime(fecha_hasta, DATE_FMT)

        if desde > hasta:
            return False, "La fecha 'desde' no puede ser posterior a la fecha 'hasta'"

        return True, ""
    except ValueError:
        return False, "Formato de fecha inválido. Use dd/mm/yyyy"

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def convert_date_format_for_calendar(date_str: str) -> str:
    """Convert date from dd/mm/yyyy (UI format) to yyyy-mm-dd (Calendar ID format).

    Args:
        date_str: Date string in format dd/mm/yyyy (e.g., "31/12/2024")

    Returns:
        Date string in format yyyy-mm-dd (e.g., "2024-12-31")

    Example:
        >>> convert_date_format_for_calendar("01/01/2024")
        "2024-01-01"
        >>> convert_date_format_for_calendar("31/12/2024")
        "2024-12-31"
    """
    try:
        # Parse dd/mm/yyyy
        day, month, year = date_str.split("/")
        # Return yyyy-mm-dd
        calendar_format = f"{year}-{month}-{day}"
        return calendar_format
    except Exception as e:
        logger.warning(f"Error converting date format '{date_str}': {e}. Returning original.")
        return date_str

async def navigate_calendar_to_date_fast(page, target_year: int, target_month: int, on_log=print):
    """Navigate v-calendar using year/month picker (faster).

    Many v-calendar implementations allow clicking the title to open a year/month picker.

    Args:
        page: Playwright page
        target_year: Target year (e.g., 2024)
        target_month: Target month (1-12)
        on_log: Logging function

    Returns:
        True if navigation successful, False if need to use fallback
    """
    try:
        # Click en el título para abrir year/month picker
        title_selector = '.vc-title, button.vc-title'
        title = page.locator(title_selector).first
        await title.wait_for(state="visible", timeout=3000)
        on_log(f"  [DEBUG] Clickeando título del calendario para abrir picker")
        await title.click()
        await asyncio.sleep(0.5)

        # Ahora debería aparecer un selector de año/mes
        # Buscar el año objetivo
        year_selector = f'button:has-text("{target_year}"), .vc-nav-item:has-text("{target_year}")'
        year_btn = page.locator(year_selector).first

        if await year_btn.count() > 0:
            on_log(f"  [DEBUG] Seleccionando año {target_year} del picker")
            await year_btn.click()
            await asyncio.sleep(0.5)

            # Seleccionar mes si es necesario
            # v-calendar podría mostrar grid de meses después de seleccionar año
            # Intentar clickear en el mes objetivo
            month_names = ["January", "February", "March", "April", "May", "June",
                          "July", "August", "September", "October", "November", "December"]
            month_names_es = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                             "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

            target_month_name_en = month_names[target_month - 1]
            target_month_name_es = month_names_es[target_month - 1]

            # Intentar ambos idiomas
            month_selector = f'button:has-text("{target_month_name_en}"), button:has-text("{target_month_name_es}"), .vc-nav-item:has-text("{target_month_name_en}"), .vc-nav-item:has-text("{target_month_name_es}")'
            month_btn = page.locator(month_selector).first

            if await month_btn.count() > 0:
                on_log(f"  [DEBUG] Seleccionando mes {target_month_name_en}/{target_month_name_es}")
                await month_btn.click()
                await asyncio.sleep(0.5)

            on_log(f"  ✓ Navegado a {target_month}/{target_year}")
            return True
        else:
            on_log(f"  [WARNING] Year picker no disponible, calendario ya debe estar en fecha correcta")
            return False

    except Exception as e:
        on_log(f"  [WARNING] No se pudo usar year picker: {e}")
        on_log(f"  [INFO] Asumiendo que calendario ya está en mes/año correcto")
        return False

async def navigate_calendar_to_date_with_arrows(page, target_year: int, target_month: int, on_log=print):
    """Navigate v-calendar to target year/month using arrow buttons ONLY.

    Does NOT click on title - only uses left/right arrows.

    Args:
        page: Playwright page
        target_year: Target year (e.g., 2024)
        target_month: Target month (1-12)
        on_log: Logging function
    """
    max_attempts = 36  # Max 36 months (3 years)
    attempts = 0

    month_names_en = ["January", "February", "March", "April", "May", "June",
                      "July", "August", "September", "October", "November", "December"]
    month_names_es = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                      "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

    # Create mapping for both languages
    month_map = {}
    for i, (en, es) in enumerate(zip(month_names_en, month_names_es), 1):
        month_map[en.lower()] = i
        month_map[es.lower()] = i
        month_map[en[:3].lower()] = i  # Short form: Jan, Feb, etc.

    while attempts < max_attempts:
        attempts += 1

        try:
            # Get current calendar month/year from title (READ ONLY - DO NOT CLICK)
            title_selector = '.vc-title'
            title_element = page.locator(title_selector).first

            # Wait for title to be visible
            await title_element.wait_for(state="visible", timeout=3000)
            title_text = await title_element.text_content(timeout=3000)

            on_log(f"  [DEBUG] Intento {attempts}: Calendario mostrando '{title_text}'")

            # Parse year from title (format: "Month YYYY" or "Month de YYYY")
            import re
            year_match = re.search(r'\b(20\d{2})\b', title_text)
            if not year_match:
                on_log(f"  [ERROR] No se pudo extraer año del título: '{title_text}'")
                raise ValueError(f"Cannot parse year from title: {title_text}")

            current_year = int(year_match.group(1))

            # Parse month from title
            title_lower = title_text.lower()
            current_month = None

            for month_name, month_num in month_map.items():
                if month_name in title_lower:
                    current_month = month_num
                    break

            if current_month is None:
                on_log(f"  [ERROR] No se pudo extraer mes del título: '{title_text}'")
                # Intentar con regex para número de mes (si está en formato MM/YYYY)
                month_match = re.search(r'\b(0?[1-9]|1[0-2])\b', title_text)
                if month_match:
                    current_month = int(month_match.group(1))
                else:
                    raise ValueError(f"Cannot parse month from title: {title_text}")

            on_log(f"  [DEBUG] Fecha actual del calendario: {current_month}/{current_year}")
            on_log(f"  [DEBUG] Fecha objetivo: {target_month}/{target_year}")

            # Check if we're at the target month/year
            if current_year == target_year and current_month == target_month:
                on_log(f"  ✓ Calendario navegado correctamente a {target_month}/{target_year}")
                return True

            # Determine direction to navigate
            current_date = current_year * 12 + current_month
            target_date = target_year * 12 + target_month

            if current_date > target_date:
                # Need to go backwards (previous month)
                arrow_selector = '.vc-arrow.is-left'
                direction = "anterior"
            else:
                # Need to go forwards (next month)
                arrow_selector = '.vc-arrow.is-right'
                direction = "siguiente"

            on_log(f"  [DEBUG] Navegando a mes {direction} con flecha {arrow_selector}...")

            # Click the arrow button
            arrow = page.locator(arrow_selector).first
            await arrow.wait_for(state="visible", timeout=3000)
            await arrow.click()

            # Wait for calendar to update
            await asyncio.sleep(0.6)  # Slightly longer wait for animation

        except Exception as e:
            on_log(f"  [ERROR] Error en intento {attempts}: {e}")

            if attempts >= max_attempts:
                raise TimeoutError(f"No se pudo navegar al mes objetivo después de {max_attempts} intentos")

            # Try to continue
            await asyncio.sleep(0.5)

    raise TimeoutError(f"No se pudo navegar al mes objetivo después de {max_attempts} intentos")

# ---------------- Checkpoint System ---------------- #

@dataclass
class BatchProgress:
    """Estado del procesamiento batch."""
    session_id: str
    cuit_login: str
    cuit_target: str
    fecha_desde: str
    fecha_hasta: str
    started_at: str
    completed_tax_codes: List[str]
    current_tax_code: Optional[str]
    all_downloaded_files: List[str]
    status: str  # "in_progress", "completed", "error"
    last_updated: str

def get_checkpoint_path(session_id: str) -> Path:
    """Get the path to the checkpoint file."""
    return OUTPUT_DIR / f"checkpoint_{session_id}.json"

def save_checkpoint(progress: BatchProgress) -> None:
    """Save progress to JSON checkpoint file."""
    progress.last_updated = now_ts()
    checkpoint_path = get_checkpoint_path(progress.session_id)

    with open(checkpoint_path, 'w', encoding='utf-8') as f:
        json.dump(asdict(progress), f, indent=2, ensure_ascii=False)

def load_checkpoint(session_id: str) -> Optional[BatchProgress]:
    """Load progress from JSON checkpoint file."""
    checkpoint_path = get_checkpoint_path(session_id)

    if not checkpoint_path.exists():
        return None

    try:
        with open(checkpoint_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return BatchProgress(**data)
    except Exception as e:
        logger.error(f"Error loading checkpoint: {e}")
        return None

def find_latest_checkpoint() -> Optional[BatchProgress]:
    """Find the most recent checkpoint file."""
    checkpoint_files = list(OUTPUT_DIR.glob("checkpoint_*.json"))

    if not checkpoint_files:
        return None

    # Sort by modification time, most recent first
    checkpoint_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Load the most recent checkpoint that's in_progress
    for checkpoint_file in checkpoint_files:
        session_id = checkpoint_file.stem.replace("checkpoint_", "")
        progress = load_checkpoint(session_id)
        if progress and progress.status == "in_progress":
            return progress

    return None

# ---------------- Scraper Core ---------------- #

async def _apply_viewport(page):
    try:
        await page.set_viewport_size({"width": 1920, "height": 1080})
    except Exception:
        pass

async def _afip_login(context, cuit_login: str, clave: str, on_log=print):
    """Login to AFIP portal."""
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

async def _open_mis_retenciones(context, portal_page, on_log=print):
    """From the AFIP portal home, open 'MIS RETENCIONES' service."""
    on_log("Buscando servicio MIS RETENCIONES...")

    # Sometimes the tile is below a 'Ver todos' link
    try:
        for sel in MR_TILE_SELECTORS:
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
    for sel in MR_TILE_SELECTORS:
        candidate = portal_page.locator(sel).first
        try:
            await candidate.wait_for(state="visible", timeout=4000)
            link = candidate
            break
        except TimeoutError:
            continue
    if link is None:
        # Search by heading text as a fallback
        link = portal_page.locator("a:has(h3:has-text('MIS RETENCIONES'))").first

    on_log("Abriendo MIS RETENCIONES...")
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

async def _select_cuit_representado(page, cuit_target: str, on_log=print):
    """Select CUIT (representado) from dropdown menu."""
    try:
        on_log(f"Buscando dropdown de representados...")

        # Click on the dropdown toggle button (user avatar/menu)
        # According to Step_2, the dropdown is: id="navBarMisRetenciones-dropdown-changeRelation"
        dropdown_btn = page.locator("#navBarMisRetenciones-dropdown-changeRelation, a[id*='dropdown-changeRelation']").first

        # First check if we need to open the user menu
        user_dropdown = page.locator("#e-navbar-dropdown-toggle, a[data-bs-toggle='dropdown']").first
        if await user_dropdown.count():
            on_log("Abriendo menú de usuario...")
            await user_dropdown.click()
            await asyncio.sleep(1)

        # Click on "Seleccionar representado"
        await dropdown_btn.wait_for(state="visible", timeout=10000)
        await dropdown_btn.click()
        await asyncio.sleep(1)
        await page.wait_for_load_state("networkidle", timeout=15000)

        on_log(f"Seleccionando CUIT objetivo: {cuit_target}...")

        # The CUIT appears in format XX-XXXXXXXX-X
        cuit_formatted = f"{cuit_target[:2]}-{cuit_target[2:10]}-{cuit_target[10]}"

        # Find the card/panel with the target CUIT
        # According to Step_3, the CUIT is in: h6.e-relation__text--cuit
        selector = f"h6.e-relation__text--cuit:has-text('{cuit_formatted}'), h6:has-text('{cuit_formatted}')"
        cuit_element = page.locator(selector).first

        if await cuit_element.count():
            # Click on the parent card
            card = cuit_element.locator("xpath=ancestor::div[contains(@class, 'e-relation__card') or contains(@class, 'card')]").first
            await card.wait_for(state="visible", timeout=5000)
            on_log(f"Seleccionando CUIT: {cuit_formatted}...")
            await card.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)
            on_log("✓ CUIT seleccionado")
        else:
            on_log(f"⚠ CUIT {cuit_formatted} no encontrado en la lista. Continuando con CUIT por defecto...")

    except TimeoutError:
        on_log("⚠ Selector de representado no encontrado; se asume CUIT por defecto.")
    except Exception as e:
        on_log(f"⚠ Error al seleccionar CUIT: {e}")

async def _fill_consulta_form(page, tax_code: str, operation_type: Optional[str], fecha_desde: str, fecha_hasta: str, on_log=print):
    """Fill the consulta form with tax type, operation type, and dates.

    Args:
        tax_code: e.g., "IMP_217"
        operation_type: "1" (Retención), "2" (Percepción), "0" (Ambas), or None (skip field)
        fecha_desde: "dd/mm/yyyy"
        fecha_hasta: "dd/mm/yyyy"
    """
    on_log("Llenando formulario de consulta...")

    # Wait for the form to load
    await page.wait_for_selector("#selectImpuestos, input#selectImpuestos", timeout=15000)

    # 1. Select tax type (Impuesto)
    on_log(f"Seleccionando impuesto: {tax_code}...")

    # Click on the multiselect dropdown
    impuesto_input = page.locator("#selectImpuestos")
    await impuesto_input.wait_for(state="visible", timeout=10000)
    await impuesto_input.click()
    await asyncio.sleep(0.5)

    # Wait for dropdown to open
    await page.wait_for_selector(f"#selectImpuestos-multiselect-option-{tax_code}", timeout=10000)

    # Click on the specific option
    option = page.locator(f"#selectImpuestos-multiselect-option-{tax_code}")
    await option.scroll_into_view_if_needed()
    await option.click()
    await asyncio.sleep(0.5)
    on_log("✓ Impuesto seleccionado")

    # 2. Select operation type (Tipo de operación) - if applicable
    if operation_type is not None:
        on_log(f"Seleccionando tipo de operación: {operation_type}...")

        # The radio buttons have values: "1" (Retención), "2" (Percepción), "0" (Retención y percepción)
        radio_selector = f"input[type='radio'][value='{operation_type}']"
        radio = page.locator(radio_selector).first

        try:
            await radio.wait_for(state="visible", timeout=5000)
            await radio.check()
            await asyncio.sleep(0.3)
            on_log("✓ Tipo de operación seleccionado")
        except TimeoutError:
            on_log(f"⚠ Campo 'Tipo de operación' no encontrado (puede ser esperado para algunos impuestos)")

    # 3. Fill dates
    on_log(f"Completando fechas: {fecha_desde} - {fecha_hasta}...")

    # Fecha desde - TIPEO MANUAL
    on_log(f"  [DEBUG] Ingresando fecha desde manualmente: {fecha_desde}")
    fecha_desde_input = page.locator("#datePickerFechasRetencionesDesde__input")
    await fecha_desde_input.wait_for(state="visible", timeout=10000)

    # Click para hacer focus
    await fecha_desde_input.click()
    await asyncio.sleep(0.3)

    # CRITICAL: Clear primero, luego fill
    await fecha_desde_input.fill("")  # Clear
    await asyncio.sleep(0.2)
    await fecha_desde_input.fill(fecha_desde)  # dd/mm/yyyy
    await asyncio.sleep(0.5)

    # NO usar Tab - simplemente hacer click afuera para confirmar
    await page.locator("body").click(position={"x": 0, "y": 0})  # Click en esquina superior
    await asyncio.sleep(0.3)

    on_log(f"  ✓ Fecha desde ingresada: {fecha_desde}")

    # Fecha hasta - CON NAVEGACIÓN EN CALENDARIO
    on_log(f"  [DEBUG] Seleccionando fecha hasta con calendario: {fecha_hasta}")
    fecha_hasta_input = page.locator("#datePickerFechasRetencionesHasta__input")
    await fecha_hasta_input.wait_for(state="visible", timeout=10000)

    # Abrir calendario
    on_log(f"  [DEBUG] Abriendo calendario 'Fecha hasta'...")
    await fecha_hasta_input.click()
    await asyncio.sleep(0.5)

    # Esperar que se renderice
    on_log(f"  [DEBUG] Esperando que se renderice el calendario...")
    await page.wait_for_selector('.vc-pane-container', state='visible', timeout=10000)
    await asyncio.sleep(0.5)

    # Parsear fecha objetivo
    fecha_obj = datetime.strptime(fecha_hasta, "%d/%m/%Y")
    target_year = fecha_obj.year
    target_month = fecha_obj.month

    # Navegar con flechas directamente (sin intentar year picker)
    on_log(f"  [DEBUG] Navegando al mes objetivo: {target_month}/{target_year}")
    await navigate_calendar_to_date_with_arrows(page, target_year, target_month, on_log)

    # Ahora el calendario DEBE estar en el mes correcto
    # Verificar que el día existe antes de hacer click
    fecha_hasta_calendar = convert_date_format_for_calendar(fecha_hasta)
    day_selector_hasta = f'.vc-day.id-{fecha_hasta_calendar}'

    on_log(f"  [DEBUG] Verificando si día {fecha_hasta_calendar} está visible...")
    day_element = page.locator(day_selector_hasta)

    if await day_element.count() == 0:
        on_log(f"  [ERROR] Día {fecha_hasta_calendar} no encontrado en DOM después de navegación")
        # Dump HTML for debugging
        calendar_html = await page.locator('.vc-pane-container').inner_html()
        on_log(f"  [DEBUG] HTML del calendario:\n{calendar_html[:500]}")
        raise ValueError(f"Día {fecha_hasta_calendar} no existe en el calendario")

    on_log(f"  [DEBUG] Haciendo click en día: {day_selector_hasta}")
    await day_element.click(timeout=5000)
    await asyncio.sleep(0.5)
    on_log(f"  ✓ Fecha hasta seleccionada: {fecha_hasta}")

    on_log("✓ Formulario completado")

async def _click_consultar(page, on_log=print):
    """Click the 'Consultar' button and wait for results."""
    on_log("Haciendo click en Consultar...")

    consultar_btn = page.locator("#btnConsultarRetenciones, button#btnConsultarRetenciones")
    await consultar_btn.wait_for(state="visible", timeout=10000)
    await consultar_btn.scroll_into_view_if_needed()
    await consultar_btn.click()

    # Wait for results to load
    on_log("Esperando resultados...")
    await asyncio.sleep(3)

    # Wait for the export button to appear (indicates results are loaded)
    try:
        await page.wait_for_selector("#btnExportarOtrosFormatos, button#btnExportarOtrosFormatos", timeout=30000)
        on_log("✓ Resultados cargados")
    except TimeoutError:
        on_log("⚠ Botón de exportar no apareció. Es posible que no haya resultados.")

async def _export_csv(page, on_log=print):
    """Click the Export dropdown and select CSV."""
    on_log("Exportando a CSV...")

    # Click on the Export dropdown button
    on_log("  [DEBUG] Buscando botón 'Exportar'...")
    export_btn = page.locator("#btnExportarOtrosFormatos, button#btnExportarOtrosFormatos")
    await export_btn.wait_for(state="visible", timeout=15000)
    await export_btn.scroll_into_view_if_needed()
    on_log("  [DEBUG] Click en botón 'Exportar'...")
    await export_btn.click()
    await asyncio.sleep(1)

    # Wait for dropdown menu to appear
    on_log("  [DEBUG] Esperando menú dropdown...")
    await page.wait_for_selector(".dropdown-menu a.dropdown-item:has-text('.CSV')", timeout=10000)
    on_log("  [DEBUG] Menú dropdown visible")

    # Click on CSV option
    on_log("  [DEBUG] Click en opción '.CSV'...")
    csv_option = page.locator(".dropdown-menu a.dropdown-item:has-text('.CSV')").first
    await csv_option.click()

    # Wait longer for the modal to appear (it takes 3-5 seconds)
    on_log("  [DEBUG] Esperando a que aparezca el modal de exportación (puede tomar 5-7 segundos)...")
    await asyncio.sleep(4)

    on_log("✓ Click en CSV completado - exportación iniciada")

async def _handle_export_popup(page, on_log=print):
    """Navigate to 'Consultas exportadas' tab after export.

    NOTE: The modal popup usually appears after 3-5 seconds after clicking CSV export.
    We need to wait for it and click "Ver archivo" button.
    """
    on_log("[DEBUG] Verificando si aparece popup de exportación...")

    # Try multiple selectors for the modal with extended timeout
    modal_appeared = False
    modal_selectors = [
        "#modal-sinresultados",  # Main modal container
        "#modal-sinresultados_content",  # Modal content
        ".modal.show",  # Bootstrap modal with show class
        ".modal-content",  # Generic modal content
    ]

    for idx, selector in enumerate(modal_selectors, 1):
        try:
            on_log(f"  [DEBUG] Probando selector de modal #{idx}: {selector} (timeout: 10s)")
            await page.wait_for_selector(selector, state="visible", timeout=10000)
            modal_appeared = True
            on_log(f"✓ Popup apareció con selector: {selector}")
            break
        except TimeoutError:
            on_log(f"  [DEBUG] Selector #{idx} timeout: {selector}")
            continue

    if not modal_appeared:
        on_log("  [DEBUG] Popup NO apareció después de probar todos los selectores")

    if modal_appeared:
        # Modal appeared, click "Ver archivo" button
        on_log("Buscando botón 'Ver archivo' en popup...")

        selectors_to_try = [
            "#modal-sinresultados_btnOK",  # Most specific
            "#modal-sinresultados_footer button.btn-primary",  # In footer
            "#modal-sinresultados button.btn-primary",  # Anywhere in modal
            ".modal-content button.btn-primary",  # Generic modal
            "button:has-text('Ver archivo')",  # By text
            ".modal.show button:has-text('Ver archivo')",  # In visible modal by text
        ]

        ver_archivo_btn = None
        for idx, selector in enumerate(selectors_to_try, 1):
            try:
                on_log(f"  [DEBUG] Probando selector de botón #{idx}: {selector}")
                candidate = page.locator(selector).first
                count = await candidate.count()
                on_log(f"  [DEBUG] Selector #{idx} encontró {count} elemento(s)")

                if count > 0:
                    await candidate.wait_for(state="visible", timeout=5000)
                    ver_archivo_btn = candidate
                    on_log(f"✓ Botón 'Ver archivo' encontrado con selector #{idx}: {selector}")
                    break
            except Exception as e:
                on_log(f"  [DEBUG] Selector #{idx} falló: {e}")
                continue

        if ver_archivo_btn:
            await ver_archivo_btn.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            on_log("Haciendo click en botón 'Ver archivo'...")
            await ver_archivo_btn.click()
            on_log("  [DEBUG] Click realizado, esperando navegación...")
            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2)
            on_log("✓ Navegado a 'Consultas exportadas' (vía popup)")
            return
        else:
            on_log("⚠ No se encontró el botón 'Ver archivo' con ningún selector. Intentando navegación manual...")

    # New behavior (default): No popup, manually navigate to "Consultas exportadas" tab
    on_log("Navegando manualmente a tab 'Consultas exportadas'...")

    # Wait a bit for the export to be registered
    await asyncio.sleep(2)

    # Try to find and click the "Consultas exportadas" tab
    tab_selectors = [
        "button#tabConsultasExportdas-tab",
        "button[aria-controls='tabConsultasExportdas']",
        "button:has-text('Consultas exportadas')",
        "a:has-text('Consultas exportadas')",
        "[role='tab']:has-text('Consultas exportadas')",
    ]

    on_log("  [DEBUG] Buscando tab 'Consultas exportadas'...")
    tab_found = False

    for idx, selector in enumerate(tab_selectors, 1):
        try:
            on_log(f"  [DEBUG] Probando selector #{idx}: {selector}")
            tab = page.locator(selector).first
            if await tab.count() > 0:
                on_log(f"  [DEBUG] Tab encontrada con selector #{idx}")
                await tab.wait_for(state="visible", timeout=5000)
                await tab.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)
                on_log(f"  [DEBUG] Click en tab 'Consultas exportadas'...")
                await tab.click()
                await asyncio.sleep(2)
                await page.wait_for_load_state("networkidle", timeout=15000)
                tab_found = True
                on_log("✓ Navegado a 'Consultas exportadas' (vía tab)")
                break
        except Exception as e:
            on_log(f"  [DEBUG] Selector #{idx} falló: {e}")
            continue

    if not tab_found:
        on_log("⚠ No se pudo encontrar tab 'Consultas exportadas' - continuando de todas formas...")

    await asyncio.sleep(2)

async def _wait_and_download_file(page, tax_code: str, cuit_target: str, fecha_desde: str, fecha_hasta: str, on_log=print, max_wait_minutes=10):
    """Wait for file using timing-based matching.

    Strategy:
    1. Wait 5 seconds after export for file to appear
    2. Get the FIRST row (row-index="0" = most recent)
    3. Verify timestamp is within last 2 minutes
    4. Verify tax number matches
    5. Download
    """
    on_log("Esperando a que el archivo esté listo...")

    # Click on "Consultas exportadas" tab if not already there
    try:
        tab_btn = page.locator("button#tabConsultasExportdas-tab, button[aria-controls='tabConsultasExportdas']").first
        if await tab_btn.count():
            await tab_btn.click()
            await asyncio.sleep(2)
    except Exception:
        pass

    tax_number = tax_code.split("_")[1] if "_" in tax_code else tax_code

    max_attempts = max_wait_minutes * 6  # 6 attempts per minute
    attempt = 0

    # Wait before first check
    on_log("  [DEBUG] Esperando 5 segundos iniciales para que el archivo se registre...")
    await asyncio.sleep(5)

    # Get current time for timestamp validation
    export_time = datetime.now()

    while attempt < max_attempts:
        attempt += 1
        on_log(f"Intento {attempt}/{max_attempts}: Buscando archivo...")

        # Refresh
        try:
            refresh_btn = page.locator("#btnRecargarTablaAplicativo, button#btnRecargarTablaAplicativo").first
            await refresh_btn.wait_for(state="visible", timeout=5000)
            await refresh_btn.click()
            await asyncio.sleep(2)
        except Exception as e:
            on_log(f"⚠ Botón refresh no encontrado: {e}")

        try:
            # Get ONLY the first row (most recent by definition)
            first_row_selector = ".ag-row[row-index='0']"
            first_row = page.locator(first_row_selector).first

            if await first_row.count() == 0:
                on_log("⚠ No hay filas en la tabla")
                await asyncio.sleep(10)
                continue

            on_log("  [DEBUG] Analizando primera fila (más reciente)...")

            # Get cells
            filtros_text = ""
            estado_text = ""
            timestamp_text = ""

            try:
                filtros_cell = first_row.locator("[col-id='filtros']")
                filtros_text = await filtros_cell.text_content(timeout=3000)
                on_log(f"  [DEBUG] Filtros: '{filtros_text}'")
            except Exception as e:
                on_log(f"  [DEBUG] Error leyendo filtros: {e}")

            try:
                estado_cell = first_row.locator("[col-id='estado']")
                estado_text = await estado_cell.text_content(timeout=3000)
                on_log(f"  [DEBUG] Estado: '{estado_text}'")
            except Exception as e:
                on_log(f"  [DEBUG] Error leyendo estado: {e}")

            try:
                timestamp_cell = first_row.locator("[col-id='fechaTimestamp']")
                timestamp_text = await timestamp_cell.text_content(timeout=3000)
                on_log(f"  [DEBUG] Timestamp: '{timestamp_text}'")
            except Exception as e:
                on_log(f"  [DEBUG] Error leyendo timestamp: {e}")

            # Matching criteria:
            # 1. Tax number in filtros
            # 2. Estado = "Finalizado"
            # 3. Timestamp is within last 5 minutes (to be safe)

            has_tax = tax_number in filtros_text if filtros_text else False
            is_finalizado = "Finalizado" in estado_text if estado_text else False

            # Parse timestamp to verify it's recent
            is_recent = False
            if timestamp_text:
                try:
                    # Format: "15/11/2025 19:51"
                    file_timestamp = datetime.strptime(timestamp_text.strip(), "%d/%m/%Y %H:%M")
                    time_diff = (export_time - file_timestamp).total_seconds() / 60  # minutes

                    # File should be created AFTER we started (negative diff) or within last 5 minutes
                    is_recent = abs(time_diff) <= 5

                    on_log(f"  [DEBUG] Tiempo desde export: {time_diff:.1f} minutos")
                    on_log(f"  [DEBUG] ¿Es reciente? {is_recent}")
                except Exception as e:
                    on_log(f"  [DEBUG] Error parseando timestamp: {e}")
                    # If can't parse, assume it's the right one (first row should be ours)
                    is_recent = True

            on_log(f"  [DEBUG] Matching: tax={has_tax}, finalizado={is_finalizado}, recent={is_recent}")

            # Match: first row + tax number + finalizado + recent timestamp
            if has_tax and is_finalizado and is_recent:
                on_log(f"✓ Archivo encontrado y listo!")

                # Find download button/link
                # The <a> tag has the download attribute
                download_link = first_row.locator("a[download]").first

                if await download_link.count() == 0:
                    on_log("  [ERROR] No se encontró link de descarga")
                    await asyncio.sleep(10)
                    continue

                # Get the download filename from the <a> tag
                download_filename = await download_link.get_attribute("download")
                on_log(f"  [DEBUG] Archivo a descargar: {download_filename}")

                # Click the button inside the <a> tag
                download_btn = download_link.locator("button").first

                on_log("  [DEBUG] Iniciando descarga...")
                try:
                    async with page.expect_download(timeout=30000) as download_info:
                        await download_btn.click()
                        download = await download_info.value

                    # Generate our own filename with dates
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    desde_fmt = fecha_desde.replace("/", "")
                    hasta_fmt = fecha_hasta.replace("/", "")
                    filename = f"MR_{tax_code}_{cuit_target}_{desde_fmt}_{hasta_fmt}_{timestamp}.csv"
                    save_path = OUTPUT_DIR / filename

                    # Save
                    on_log(f"  [DEBUG] Guardando archivo como: {save_path}")
                    await download.save_as(save_path)
                    on_log(f"✓ Archivo descargado: {save_path}")

                    return str(save_path)

                except TimeoutError:
                    on_log("  [ERROR] Timeout esperando descarga")
                    await asyncio.sleep(10)
                    continue

            elif not is_finalizado and has_tax:
                on_log(f"⚠ Nuestro archivo encontrado pero estado: '{estado_text}'")
            elif not is_recent:
                on_log(f"⚠ Archivo encontrado pero no es reciente (timestamp: {timestamp_text})")
            else:
                on_log(f"⚠ Primera fila no coincide con nuestra consulta")

        except Exception as e:
            import traceback
            on_log(f"⚠ Error: {e}")
            on_log(f"  [DEBUG] Traceback:\n{traceback.format_exc()}")

        await asyncio.sleep(10)

    raise TimeoutError(f"Archivo no listo después de {max_wait_minutes} minutos")

async def scrape_mis_retenciones(
    cuit_login: str,
    clave: str,
    cuit_target: str,
    tax_code: str,
    fecha_desde: str,
    fecha_hasta: str,
    on_log=print
):
    """Main scraper function for Mis Retenciones.

    Args:
        cuit_login: CUIT para login
        clave: Contraseña
        cuit_target: CUIT objetivo (representado)
        tax_code: Código del impuesto (e.g., "IMP_217")
        fecha_desde: Fecha desde en formato dd/mm/yyyy
        fecha_hasta: Fecha hasta en formato dd/mm/yyyy
    """
    # Validate date range
    es_valido, mensaje = validar_rango_fecha(fecha_desde, fecha_hasta)
    if not es_valido:
        on_log(f"❌ Error de validación: {mensaje}")
        raise ValueError(mensaje)

    # Get tax configuration
    tax_config = TAX_TYPES_DICT.get(tax_code)
    if not tax_config:
        raise ValueError(f"Tipo de impuesto inválido: {tax_code}")

    on_log(f"Procesando: {tax_config.name}")
    on_log(f"Modo de operación: {tax_config.operation_mode}")

    on_log("Iniciando navegador...")
    temp_profile = Path(tempfile.gettempdir()) / f"studioai_mr_{uuid.uuid4().hex[:8]}"
    temp_profile.mkdir(parents=True, exist_ok=True)
    temp_dir = str(temp_profile)
    on_log(f"Usando profile temporal: {temp_dir}")

    downloaded_files = []

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

            # Open MIS RETENCIONES
            mr_page = await _open_mis_retenciones(context, portal, on_log=on_log)

            # Select CUIT target (representado)
            await _select_cuit_representado(mr_page, cuit_target, on_log=on_log)

            # Determine operation type(s) based on tax configuration
            operations_to_run = []

            if tax_config.operation_mode == "retencion":
                operations_to_run = [("1", "Retención")]
            elif tax_config.operation_mode == "percepcion":
                operations_to_run = [("2", "Percepción")]
            elif tax_config.operation_mode == "ambas":
                operations_to_run = [("0", "Retención y percepción")]
            elif tax_config.operation_mode == "ambas_separadas":
                # Do 2 separate queries: first retención, then percepción
                operations_to_run = [("1", "Retención"), ("2", "Percepción")]
            elif tax_config.operation_mode == "fecha_solo":
                # No operation type field for aduaneras
                operations_to_run = [(None, "Solo fecha")]

            # Process each operation
            for op_value, op_name in operations_to_run:
                on_log("")
                on_log("=" * 60)
                on_log(f"PROCESANDO: {tax_config.name} - {op_name}")
                on_log("=" * 60)

                # Fill the form
                await _fill_consulta_form(mr_page, tax_code, op_value, fecha_desde, fecha_hasta, on_log=on_log)

                # Click Consultar
                await _click_consultar(mr_page, on_log=on_log)

                # Export to CSV
                await _export_csv(mr_page, on_log=on_log)

                # Handle popup and navigate to "Consultas exportadas"
                await _handle_export_popup(mr_page, on_log=on_log)

                # Wait for file and download
                file_path = await _wait_and_download_file(
                    mr_page, tax_code, cuit_target, fecha_desde, fecha_hasta, on_log=on_log
                )
                downloaded_files.append(file_path)

                # If there are more operations, navigate back to "Nueva consulta"
                if len(operations_to_run) > 1 and op_value != operations_to_run[-1][0]:
                    on_log("Navegando a 'Nueva consulta' para siguiente operación...")
                    nueva_consulta_tab = mr_page.locator("button#tabNuevaConsulta-tab, button[aria-controls='tabNuevaConsulta']").first
                    await nueva_consulta_tab.click()
                    await asyncio.sleep(2)

            # COMPLETADO
            on_log("")
            on_log("=" * 60)
            on_log("✅ PROCESO COMPLETADO 🎉")
            on_log("=" * 60)
            for idx, file in enumerate(downloaded_files, 1):
                on_log(f"Archivo {idx}: {file}")

            return {
                "files": downloaded_files,
                "tax_type": tax_config.name
            }

    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Perfil temporal eliminado: {temp_dir}")
        except Exception as e:
            logger.warning(f"No se pudo eliminar perfil temporal: {e}")

async def _navigate_to_nueva_consulta(page, on_log=print):
    """Navigate back to 'Nueva consulta' tab after processing."""
    try:
        on_log("Navegando a 'Nueva consulta'...")
        nueva_consulta_tab = page.locator("button#tabNuevaConsulta-tab, button[aria-controls='tabNuevaConsulta']").first
        await nueva_consulta_tab.wait_for(state="visible", timeout=10000)
        await nueva_consulta_tab.click()
        await asyncio.sleep(2)
        on_log("✓ Navegado a 'Nueva consulta'")
    except Exception as e:
        on_log(f"⚠ Error al navegar a 'Nueva consulta': {e}")

async def scrape_mis_retenciones_batch(
    cuit_login: str,
    clave: str,
    cuit_target: str,
    fecha_desde: str,
    fecha_hasta: str,
    resume_session_id: Optional[str] = None,
    on_log=print
):
    """Batch scraper that processes all 14 tax types automatically.

    Args:
        cuit_login: CUIT para login
        clave: Contraseña
        cuit_target: CUIT objetivo (representado)
        fecha_desde: Fecha desde en formato dd/mm/yyyy
        fecha_hasta: Fecha hasta en formato dd/mm/yyyy
        resume_session_id: Optional session ID to resume from checkpoint
    """
    # Validate date range
    es_valido, mensaje = validar_rango_fecha(fecha_desde, fecha_hasta)
    if not es_valido:
        on_log(f"❌ Error de validación: {mensaje}")
        raise ValueError(mensaje)

    # Check if resuming from checkpoint
    progress = None
    if resume_session_id:
        progress = load_checkpoint(resume_session_id)
        if progress:
            on_log(f"📂 Reanudando desde checkpoint: {resume_session_id}")
            on_log(f"Completados: {len(progress.completed_tax_codes)}/{len(TAX_TYPES)}")
        else:
            on_log(f"⚠ No se encontró checkpoint para: {resume_session_id}")

    # Create new progress if not resuming
    if not progress:
        session_id = uuid.uuid4().hex[:12]
        progress = BatchProgress(
            session_id=session_id,
            cuit_login=cuit_login,
            cuit_target=cuit_target,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            started_at=now_ts(),
            completed_tax_codes=[],
            current_tax_code=None,
            all_downloaded_files=[],
            status="in_progress",
            last_updated=now_ts()
        )
        save_checkpoint(progress)
        on_log(f"🆕 Nueva sesión batch: {session_id}")

    on_log("")
    on_log("=" * 70)
    on_log(f"MODO BATCH: Procesando {len(TAX_TYPES)} tipos de impuestos")
    on_log("=" * 70)
    on_log("")

    on_log("Iniciando navegador...")
    temp_profile = Path(tempfile.gettempdir()) / f"studioai_mr_batch_{progress.session_id}"
    temp_profile.mkdir(parents=True, exist_ok=True)
    temp_dir = str(temp_profile)

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

            # Open MIS RETENCIONES
            mr_page = await _open_mis_retenciones(context, portal, on_log=on_log)

            # Select CUIT target (representado)
            await _select_cuit_representado(mr_page, cuit_target, on_log=on_log)

            # Process each tax type
            for idx, tax_config in enumerate(TAX_TYPES, 1):
                # Skip if already completed
                if tax_config.code in progress.completed_tax_codes:
                    on_log(f"⏭️  [{idx}/{len(TAX_TYPES)}] Saltando {tax_config.name} (ya completado)")
                    continue

                on_log("")
                on_log("=" * 70)
                on_log(f"📋 [{idx}/{len(TAX_TYPES)}] PROCESANDO: {tax_config.name}")
                on_log(f"Categoría: {tax_config.category}")
                on_log(f"Modo: {tax_config.operation_mode}")
                on_log("=" * 70)

                # Update current tax in progress
                progress.current_tax_code = tax_config.code
                save_checkpoint(progress)

                # Determine operation type(s) based on tax configuration
                operations_to_run = []

                if tax_config.operation_mode == "retencion":
                    operations_to_run = [("1", "Retención")]
                elif tax_config.operation_mode == "percepcion":
                    operations_to_run = [("2", "Percepción")]
                elif tax_config.operation_mode == "ambas":
                    operations_to_run = [("0", "Retención y percepción")]
                elif tax_config.operation_mode == "ambas_separadas":
                    # Do 2 separate queries: first retención, then percepción
                    operations_to_run = [("1", "Retención"), ("2", "Percepción")]
                elif tax_config.operation_mode == "fecha_solo":
                    # No operation type field for aduaneras
                    operations_to_run = [(None, "Solo fecha")]

                # Process each operation for this tax type
                for op_value, op_name in operations_to_run:
                    on_log(f"  → {op_name}")

                    try:
                        # Fill the form
                        await _fill_consulta_form(mr_page, tax_config.code, op_value, fecha_desde, fecha_hasta, on_log=on_log)

                        # Click Consultar
                        await _click_consultar(mr_page, on_log=on_log)

                        # Export to CSV
                        await _export_csv(mr_page, on_log=on_log)

                        # Handle popup and navigate to "Consultas exportadas"
                        await _handle_export_popup(mr_page, on_log=on_log)

                        # Wait for file and download
                        file_path = await _wait_and_download_file(
                            mr_page, tax_config.code, cuit_target, fecha_desde, fecha_hasta, on_log=on_log
                        )

                        progress.all_downloaded_files.append(file_path)
                        save_checkpoint(progress)

                        on_log(f"  ✓ {op_name} completado")

                        # Navigate back to "Nueva consulta" for next operation/tax type
                        if op_value != operations_to_run[-1][0] or idx < len(TAX_TYPES):
                            await _navigate_to_nueva_consulta(mr_page, on_log=on_log)

                    except Exception as e:
                        on_log(f"  ❌ Error en {op_name}: {e}")
                        # Continue with next operation
                        try:
                            await _navigate_to_nueva_consulta(mr_page, on_log=on_log)
                        except:
                            pass

                # Mark this tax type as completed
                progress.completed_tax_codes.append(tax_config.code)
                progress.current_tax_code = None
                save_checkpoint(progress)

                on_log(f"✅ [{idx}/{len(TAX_TYPES)}] {tax_config.name} COMPLETADO")
                on_log(f"Progreso general: {len(progress.completed_tax_codes)}/{len(TAX_TYPES)}")

            # All completed
            progress.status = "completed"
            save_checkpoint(progress)

            on_log("")
            on_log("=" * 70)
            on_log("🎉 BATCH PROCESS COMPLETADO 🎉")
            on_log("=" * 70)
            on_log(f"Total de archivos descargados: {len(progress.all_downloaded_files)}")
            on_log(f"Tipos de impuestos procesados: {len(progress.completed_tax_codes)}")
            on_log("")
            on_log("Archivos:")
            for idx, file_path in enumerate(progress.all_downloaded_files, 1):
                on_log(f"  {idx}. {file_path}")

            return {
                "session_id": progress.session_id,
                "files": progress.all_downloaded_files,
                "completed_count": len(progress.completed_tax_codes),
                "total_count": len(TAX_TYPES)
            }

    except Exception as e:
        progress.status = "error"
        save_checkpoint(progress)
        on_log(f"❌ Error en batch process: {e}")
        raise

    finally:
        try:
            if progress.status == "completed":
                # Clean up temp profile only if completed successfully
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Perfil temporal eliminado: {temp_dir}")
            else:
                on_log(f"⚠ Perfil temporal conservado para posible reanudación: {temp_dir}")
        except Exception as e:
            logger.warning(f"No se pudo eliminar perfil temporal: {e}")

# ---------------- Tkinter GUI ---------------- #

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AFIP - MIS RETENCIONES")
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

        # Tax type dropdown
        ttk.Label(frm, text="Tipo de Impuesto *").grid(row=3, column=0, sticky="w")
        self.tax_var = tk.StringVar()
        self.tax_combo = ttk.Combobox(frm, textvariable=self.tax_var, width=40, state="readonly")

        # Populate dropdown with tax types grouped by category
        tax_options = []
        for tax in TAX_TYPES:
            tax_options.append(f"{tax.name} [{tax.category}]")

        self.tax_combo['values'] = tax_options
        self.tax_combo.grid(row=3, column=1, padx=8, pady=6, columnspan=2)

        # Fecha desde
        ttk.Label(frm, text="Fecha desde (dd/mm/yyyy) *").grid(row=4, column=0, sticky="w")
        self.e_fecha_desde = ttk.Entry(frm, width=22)
        self.e_fecha_desde.grid(row=4, column=1, padx=8, pady=6)
        # Set default to 30 days ago
        fecha_desde_default = (datetime.now() - timedelta(days=30)).strftime(DATE_FMT)
        self.e_fecha_desde.insert(0, fecha_desde_default)

        # Fecha hasta
        ttk.Label(frm, text="Fecha hasta (dd/mm/yyyy) *").grid(row=5, column=0, sticky="w")
        self.e_fecha_hasta = ttk.Entry(frm, width=22)
        self.e_fecha_hasta.grid(row=5, column=1, padx=8, pady=6)
        # Set default to today
        fecha_hasta_default = datetime.now().strftime(DATE_FMT)
        self.e_fecha_hasta.insert(0, fecha_hasta_default)

        # Batch mode checkbox
        self.batch_mode_var = tk.BooleanVar(value=False)
        self.batch_checkbox = ttk.Checkbutton(
            frm,
            text="Procesar TODOS los tipos de impuestos (Modo Batch)",
            variable=self.batch_mode_var,
            command=self.on_batch_mode_changed
        )
        self.batch_checkbox.grid(row=6, column=0, columnspan=3, sticky="w", padx=8, pady=(10, 6))

        # Start button
        self.btn = ttk.Button(frm, text="Iniciar", command=self.on_start)
        self.btn.grid(row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=6)

        # Resume button
        self.btn_resume = ttk.Button(frm, text="Reanudar última sesión", command=self.on_resume)
        self.btn_resume.grid(row=7, column=2, sticky="ew", padx=8, pady=6)
        self.btn_resume.config(state="disabled")  # Disabled by default

        # Check if there's a checkpoint to resume
        self.check_for_checkpoint()

        # Log area
        ttk.Label(frm, text="Log:").grid(row=8, column=0, columnspan=3, sticky="w")
        self.log = tk.Text(frm, height=16, width=80, state="disabled", wrap="word")
        self.log.grid(row=9, column=0, columnspan=3, padx=8, pady=6)
        scroll = ttk.Scrollbar(frm, command=self.log.yview)
        scroll.grid(row=9, column=3, sticky="ns")
        self.log.config(yscrollcommand=scroll.set)

    def log_line(self, msg: str):
        self.log.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.update_idletasks()

    def on_batch_mode_changed(self):
        """Handle batch mode checkbox change."""
        if self.batch_mode_var.get():
            # Batch mode: disable tax type dropdown
            self.tax_combo.config(state="disabled")
            self.log_line("Modo Batch activado: Se procesarán todos los tipos de impuestos")
        else:
            # Single mode: enable tax type dropdown
            self.tax_combo.config(state="readonly")
            self.log_line("Modo Single activado: Seleccione un tipo de impuesto")

    def check_for_checkpoint(self):
        """Check if there's a checkpoint file to resume from."""
        latest = find_latest_checkpoint()
        if latest:
            self.btn_resume.config(state="normal")
            self.log_line(f"Sesión interrumpida encontrada: {latest.session_id}")
            self.log_line(f"Progreso: {len(latest.completed_tax_codes)}/{len(TAX_TYPES)} completados")
        else:
            self.btn_resume.config(state="disabled")

    def on_resume(self):
        """Resume from the latest checkpoint."""
        latest = find_latest_checkpoint()
        if not latest:
            messagebox.showerror("Error", "No se encontró ninguna sesión para reanudar.")
            return

        # Populate fields from checkpoint
        self.e_cuit.delete(0, tk.END)
        self.e_cuit.insert(0, latest.cuit_login)

        self.e_cuit_target.delete(0, tk.END)
        self.e_cuit_target.insert(0, latest.cuit_target)

        self.e_fecha_desde.delete(0, tk.END)
        self.e_fecha_desde.insert(0, latest.fecha_desde)

        self.e_fecha_hasta.delete(0, tk.END)
        self.e_fecha_hasta.insert(0, latest.fecha_hasta)

        # Enable batch mode (resume is always batch)
        self.batch_mode_var.set(True)
        self.on_batch_mode_changed()

        # Ask for confirmation
        confirm = messagebox.askyesno(
            "Reanudar sesión",
            f"Se reanudará la sesión {latest.session_id}\n\n"
            f"Iniciada: {latest.started_at}\n"
            f"Progreso: {len(latest.completed_tax_codes)}/{len(TAX_TYPES)} completados\n\n"
            f"¿Continuar?"
        )

        if confirm:
            self.log_line(f"Reanudando sesión: {latest.session_id}")
            self.start_batch_worker(resume_session_id=latest.session_id)

    def validate(self) -> Optional[Tuple[str, str, str, Optional[str], str, str]]:
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

        # If batch mode, tax_code is not required
        tax_code = None
        if not self.batch_mode_var.get():
            tax_selection = self.tax_var.get().strip()
            if not tax_selection:
                messagebox.showerror("Validación", "Debe seleccionar un tipo de impuesto.")
                return None

            # Extract tax code from selection
            # Format: "217 - SICORE-IMPTO.A LAS GANANCIAS [Impositivas]"
            # We need to find which TAX_TYPE matches this
            for tax in TAX_TYPES:
                if f"{tax.name} [{tax.category}]" == tax_selection:
                    tax_code = tax.code
                    break

            if not tax_code:
                messagebox.showerror("Validación", "Error al identificar el tipo de impuesto.")
                return None

        return cuit, clave, cuit_target, tax_code, fecha_desde, fecha_hasta

    def on_start(self):
        data = self.validate()
        if not data:
            return

        cuit, clave, cuit_target, tax_code, fecha_desde, fecha_hasta = data

        # Check if batch mode
        if self.batch_mode_var.get():
            self.start_batch_worker()
        else:
            self.start_single_worker(cuit, clave, cuit_target, tax_code, fecha_desde, fecha_hasta)

    def start_single_worker(self, cuit, clave, cuit_target, tax_code, fecha_desde, fecha_hasta):
        """Start worker for single tax type processing."""
        self.btn.configure(state="disabled", text="Procesando...")
        self.btn_resume.configure(state="disabled")
        self.log_line("✓ Validación OK. Arrancando modo single...")

        def worker():
            try:
                def _on_log(msg):
                    self.after(0, self.log_line, msg)

                result = asyncio.run(scrape_mis_retenciones(
                    cuit, clave, cuit_target, tax_code, fecha_desde, fecha_hasta, on_log=_on_log
                ))

                files_msg = "\n".join([f"- {f}" for f in result['files']])

                self.after(0, lambda: messagebox.showinfo(
                    "Listo",
                    f"Proceso finalizado.\n\n"
                    f"Tipo de impuesto: {result['tax_type']}\n\n"
                    f"Archivos descargados:\n{files_msg}"
                ))
            except Exception as e:
                err_msg = f"{e}"
                import traceback
                full_trace = traceback.format_exc()
                self.after(0, lambda m=err_msg: messagebox.showerror("Error", m))
                self.after(0, lambda t=full_trace: self.log_line(f"ERROR COMPLETO:\n{t}"))
            finally:
                self.after(0, lambda: self.btn.configure(state="normal", text="Iniciar"))
                self.after(0, lambda: self.btn_resume.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def start_batch_worker(self, resume_session_id: Optional[str] = None):
        """Start worker for batch processing all tax types."""
        data = self.validate()
        if not data:
            return

        cuit, clave, cuit_target, _, fecha_desde, fecha_hasta = data

        self.btn.configure(state="disabled", text="Procesando Batch...")
        self.btn_resume.configure(state="disabled")
        self.batch_checkbox.configure(state="disabled")

        if not resume_session_id:
            self.log_line("✓ Validación OK. Arrancando modo batch...")
        else:
            self.log_line(f"✓ Reanudando sesión batch: {resume_session_id}")

        def worker():
            try:
                def _on_log(msg):
                    self.after(0, self.log_line, msg)

                result = asyncio.run(scrape_mis_retenciones_batch(
                    cuit, clave, cuit_target, fecha_desde, fecha_hasta,
                    resume_session_id=resume_session_id,
                    on_log=_on_log
                ))

                files_count = len(result['files'])
                completed = result['completed_count']
                total = result['total_count']

                self.after(0, lambda: messagebox.showinfo(
                    "Batch Completado",
                    f"Proceso batch finalizado.\n\n"
                    f"Tipos procesados: {completed}/{total}\n"
                    f"Total archivos: {files_count}\n\n"
                    f"Ver log para detalles."
                ))

                # Re-check for checkpoints
                self.after(0, self.check_for_checkpoint)

            except Exception as e:
                err_msg = f"{e}"
                import traceback
                full_trace = traceback.format_exc()
                self.after(0, lambda m=err_msg: messagebox.showerror("Error", m))
                self.after(0, lambda t=full_trace: self.log_line(f"ERROR COMPLETO:\n{t}"))

                # Re-check for checkpoints (in case of error, might be resumable)
                self.after(0, self.check_for_checkpoint)

            finally:
                self.after(0, lambda: self.btn.configure(state="normal", text="Iniciar"))
                self.after(0, lambda: self.batch_checkbox.configure(state="normal"))
                self.after(0, lambda: self.btn_resume.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
