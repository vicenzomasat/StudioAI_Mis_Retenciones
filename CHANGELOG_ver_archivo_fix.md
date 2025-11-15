# Fix: Búsqueda robusta del botón "Ver archivo"

## Problema
El script `afip_mis_retenciones.py` tenía problemas para encontrar el botón "Ver archivo" en el popup de exportación, causando fallos intermitentes en el proceso de automatización.

## Análisis
Basándose en los archivos de guía HTML:
- `Step_9.a.Select_from_popup_menu_Ver_archivo.txt` - Estructura completa del modal
- `Step_9.bSelect_from_popup_menu_modal_menu.txt` - Contenido del modal
- `Step_9.c.Select_from_popup_menu_button_ver_archivos.txt` - Botón específico

Se identificó que el botón tiene la siguiente estructura:
```html
<button id="modal-sinresultados_btnOK" type="button" class="btn btn-primary" size="md">Ver archivo</button>
```

## Solución Implementada
Se reescribió la función `_handle_export_popup()` en `afip_mis_retenciones.py:459` con una estrategia multi-nivel de fallback que intenta 7 selectores diferentes en orden de confiabilidad:

### Estrategias de Selección:
1. **Por ID específico**: `#modal-sinresultados_btnOK` (más confiable)
2. **Por ID dentro del footer**: `#modal-sinresultados_footer button#modal-sinresultados_btnOK`
3. **Por clase dentro del footer**: `#modal-sinresultados_footer button.btn.btn-primary`
4. **Por texto dentro del contenido**: `#modal-sinresultados_content button:has-text('Ver archivo')`
5. **Por clase y texto**: `.modal-content button.btn-primary:has-text('Ver archivo')`
6. **Por texto solamente**: `button:has-text('Ver archivo')`
7. **Por texto exacto**: `button:text-is('Ver archivo')`

### Mejoras Adicionales:
- ✅ Espera adicional de 1 segundo después de que aparece el modal para asegurar renderizado completo
- ✅ Verificación de existencia y visibilidad del elemento antes de intentar click
- ✅ Scroll automático para asegurar que el botón esté en el viewport
- ✅ Logging detallado de cada estrategia intentada para debugging
- ✅ Mensaje de error claro si todas las estrategias fallan

## Beneficios:
- **Mayor robustez**: El script ahora puede manejar variaciones en la estructura del DOM
- **Mejor debugging**: Los logs muestran exactamente qué estrategia funcionó
- **Menos fallos**: Múltiples fallbacks aseguran que el botón se encuentre incluso con timing variables
- **Mantenibilidad**: Fácil agregar nuevas estrategias si es necesario

## Archivos Modificados:
- `afip_mis_retenciones.py` - Función `_handle_export_popup()` (líneas 459-523)

## Testing Recomendado:
1. Ejecutar el script en modo single con un tipo de impuesto
2. Verificar que el popup se maneja correctamente
3. Revisar los logs para confirmar qué estrategia se utilizó
4. Probar en modo batch para asegurar consistencia

## Fecha:
2025-11-15
