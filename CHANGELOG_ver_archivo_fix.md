# CHANGELOG: Fixes para Exportación y Debug Mejorado

## Fecha: 2025-11-15

---

## [COMMIT 2096b02] Fix export flow and add extensive debugging

### Análisis del Problema (basado en carpeta Fails/)

#### 1. **Popup de exportación YA NO APARECE**
**Evidencia de logs:**
```
[08:48:33] Exportando a CSV...
[08:48:35] ✓ Exportación iniciada
[08:48:35] Esperando popup de exportación...
[08:48:50] ⚠ Popup no apareció. Es posible que ya estemos en la página correcta.
```

**Análisis HTML (`Step_9.d.All_html_fail.txt`):**
- NO hay ningún elemento `<div class="modal-content">` o `#modal-sinresultados_content`
- Indica que AFIP cambió su UI: el modal ya NO aparece después de hacer click en CSV
- Comportamiento viejo: Modal con botón "Ver archivo"
- Comportamiento nuevo: Export inicia silenciosamente, sin modal

#### 2. **Script fallaba en Step 9**
- Esperaba 15 segundos por un modal que nunca aparecía
- Asumía que ya estaba en la página correcta (asunción incorrecta)
- Permanecía en página de resultados en lugar de navegar a "Consultas exportadas"
- Búsqueda de archivo posterior fallaba con timeout:
  ```
  [08:49:25] ⚠ Error al buscar archivo: Locator.text_content: Timeout 30000ms exceeded.
  ```

#### 3. **Problemas con date pickers**
**Evidencia (`Step_6.Select_date_hastal_change_of_structure_in_html_after_desde_fail.txt`):**
- Calendario quedaba abierto después de hacer click en "fecha desde"
- Interferencia con campo "fecha hasta"
- HTML mostraba calendario con `class="vc-popover-content-wrapper is-interactive"`

---

### Soluciones Implementadas

#### 1. **Manejo Mejorado de Fechas** (`_fill_consulta_form`)

**Cambios en líneas 397-442:**
```python
# ANTES:
- Click simple en campo
- Fill sin limpiar valor previo
- Espera de 0.3s
- Sin cierre de calendarios

# AHORA:
- Click en campo
- Limpieza de valor existente (fill "")
- Llenado con nueva fecha
- Presionar Tab/Escape para cerrar date picker
- Esperas de 0.5s para estabilidad
- Debug logging detallado en cada paso
```

**Mejoras:**
- ✅ Cierra calendarios automáticamente
- ✅ Evita interferencia entre campos
- ✅ Limpia valores previos antes de llenar
- ✅ Logging de cada acción para debugging

#### 2. **Reescritura Completa de Export Handler** (`_handle_export_popup`)

**ANTES (líneas 459-523):**
- Esperaba 15 segundos por modal
- Intentaba 7 estrategias para encontrar botón "Ver archivo"
- Fallaba si modal no aparecía

**AHORA (líneas 490-581) - Completamente reescrito:**

```python
# Paso 1: Verificar si modal aparece (timeout corto de 3s, no 15s)
modal_appeared = await page.wait_for_selector("#modal-sinresultados_content", timeout=3000)

# Paso 2a: Si modal aparece (comportamiento viejo)
if modal_appeared:
    # Usar estrategia antigua: buscar y hacer click en "Ver archivo"
    # 4 selectores de fallback

# Paso 2b: Si modal NO aparece (COMPORTAMIENTO NUEVO - DEFAULT)
else:
    # Navegar manualmente a tab "Consultas exportadas"
    # 5 selectores de fallback para encontrar la tab
    # Debug logging extensivo
```

**Estrategias de navegación a "Consultas exportadas":**
1. `button#tabConsultasExportdas-tab`
2. `button[aria-controls='tabConsultasExportdas']`
3. `button:has-text('Consultas exportadas')`
4. `a:has-text('Consultas exportadas')`
5. `[role='tab']:has-text('Consultas exportadas')`

**Beneficios:**
- ✅ **Retrocompatible**: Maneja tanto UI viejo como nuevo de AFIP
- ✅ **No falla si modal no aparece**: Navega manualmente
- ✅ **Debug exhaustivo**: Muestra exactamente qué selector funcionó
- ✅ **Timeout reducido**: De 15s a 3s para detección de modal

#### 3. **Búsqueda de Archivos Mejorada** (`_wait_and_download_file`)

**Cambios en líneas 627-717:**

**Problema original:**
```
[08:49:25] ⚠ Error al buscar archivo: Locator.text_content: Timeout 30000ms exceeded.
Call log:
  - waiting for locator(".ag-row[role='row']").first.locator("[col-id='filtros']")
```

**Solución:**
```python
# Manejo robusto de celdas
try:
    # Selector principal con timeout de 5s (no 30s)
    filtros_text = await filtros_cell.text_content(timeout=5000)
except:
    # Selector alternativo
    filtros_text = await first_row.locator("div.ag-cell[col-id='filtros']").text_content(timeout=5000)

# Mismo patrón para celda 'estado'
```

**Mejoras:**
- ✅ Timeout reducido de 30s a 5s por celda
- ✅ Selectores alternativos si falla el principal
- ✅ Traceback completo en caso de error
- ✅ Debug logging para cada intento
- ✅ Valida que texto no esté vacío antes de comparar

#### 4. **Debug Logging Extensivo**

**Agregado a todas las funciones críticas:**
- Prefijo `[DEBUG]` para mensajes de debugging
- Muestra selectores que se están intentando
- Reporta éxito/fallo de cada estrategia
- Incluye tracebacks completos en errores
- Muestra valores de variables críticas

**Ejemplo de output esperado:**
```
[DEBUG] Localizando campo 'Fecha desde'...
[DEBUG] Click en 'Fecha desde'...
[DEBUG] Llenando 'Fecha desde' con: 01/01/2024
[DEBUG] Presionando Tab para cerrar datepicker...
[DEBUG] Verificando si aparece popup de exportación...
[DEBUG] Popup NO apareció (comportamiento nuevo esperado)
[DEBUG] Buscando tab 'Consultas exportadas'...
[DEBUG] Probando selector #1: button#tabConsultasExportdas-tab
[DEBUG] Tab encontrada con selector #1
✓ Navegado a 'Consultas exportadas' (vía tab)
```

---

### Archivos Modificados

**afip_mis_retenciones.py:**
1. `_fill_consulta_form` (líneas 397-442) - Manejo mejorado de fechas
2. `_export_csv` (líneas 464-488) - Debug logging agregado
3. `_handle_export_popup` (líneas 490-581) - **REESCRITURA COMPLETA**
4. `_wait_and_download_file` (líneas 627-717) - Manejo robusto de errores

---

### Testing Recomendado

1. **Test básico:**
   - Ejecutar en modo single con un impuesto
   - Verificar que logs muestran el flujo completo
   - Confirmar que navega a "Consultas exportadas"

2. **Validación de debug:**
   - Revisar logs para ver qué selectores funcionaron
   - Confirmar que no hay timeouts largos
   - Verificar que muestra valores de celdas leídas

3. **Test de robustez:**
   - Probar con diferentes tipos de impuestos
   - Verificar modo batch
   - Confirmar que maneja tanto UI viejo como nuevo

4. **Casos edge:**
   - Qué pasa si tabla está vacía
   - Qué pasa si archivo no está finalizado
   - Comportamiento si modal sí aparece (retrocompatibilidad)

---

## Cambios Anteriores

### [COMMIT 1aed7be] Implement robust multi-level selector strategy for "Ver archivo" button

**Nota**: Este commit implementó la estrategia multi-nivel original, pero asumía que el popup siempre aparecería. El commit 2096b02 (arriba) reemplaza esta lógica con una solución más robusta que maneja ambos casos (con y sin popup).

---

## Próximos Pasos Sugeridos

1. **Monitorear logs** de ejecuciones para ver qué selectores funcionan consistentemente
2. **Optimizar selectores** basándose en patrones de éxito
3. **Considerar screenshots** automáticos en puntos de fallo para debug visual
4. **Agregar retry logic** si navegación a tab falla
