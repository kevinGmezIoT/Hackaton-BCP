# Vigía · Contrato de integración con el puente de la demo

La integración con el frontend original, incluida la configuración entre servidores y las revisiones de propuestas, está descrita en [Compatibilidad con FrontendHackatonCIO](compatibilidad-frontend.md).

El navegador muestra los datos del puente del equipo de UI. Vigía es cliente HTTP del puente. El monitor abre el caso en Vigía cuando el tráfico de prueba muestra una degradación. Para el recorrido principal, el primer evento publicado por Vigía da a conocer el caso a la UI.

## Arranque de la integración implementada

1. El equipo de UI arranca su puente con `npm run puente` y comparte su URL accesible desde la laptop de Vigía.
2. En el `.env` de Vigía, completar `VIGIA_BRIDGE_URL` y `VIGIA_BRIDGE_TOKEN`. Las rutas `VIGIA_BRIDGE_EVENTS_PATH`, `VIGIA_BRIDGE_DECISION_PATH` y `VIGIA_BRIDGE_METRICS_PATH` admiten prefijos distintos por instalación. Conservar `{caso}` en la ruta de decisión. Si ambos procesos viven en la misma laptop, usar puertos distintos. Para conservar casos y entregas entre arranques, asignar una ruta estable a `VIGIA_DB_PATH`. Un valor vacío selecciona una base temporal. [Variables de ejemplo](../.env.example)
3. Para ensayar con el escenario incluido, ejecutar `uv run vigia --serve --profile demo --mode assisted --demo-offline`. Para datos del servidor de la HU, arrancar con `uv run vigia --serve --profile pilot --mode assisted` y configurar modelo, catálogo, fuentes y repositorios en el entorno. El diagrama de cada servicio se resuelve mediante su entrada de catálogo. El servidor escucha en `VIGIA_HOST` y `VIGIA_PORT`.
4. Enviar desde el monitor el webhook de alerta a Vigía. El envío de eventos y la espera de la decisión corren en hilos independientes del investigador. El adaptador guarda en SQLite el último evento entregado por caso. Cada publicación lleva `X-Vigia-Token`; la decisión se atribuye al canal `puente-ui` porque el contrato mínimo no identifica al operador individual.
5. El generador de usuarios es el emisor propuesto de muestras crudas. Debe cargar en su propio entorno la URL, el token y la ruta de métricas. La integración de Vigía incluye un cliente para publicar un lote explícito y evita reenviar muestras automáticamente. Los datos sintéticos de la demo y las series p95 agregadas no se convierten en solicitudes individuales.

**Estado de la ejecución.** El ejecutor disponible es simulado. La UI recibe `simulada: true` en el paso de acción y en su verificación. Un webhook hacia el servidor de la HU requerirá el contrato del pipeline. La recepción de una aprobación en el puente no revierte ese servidor por sí misma.

## Responsabilidades

| Emisor | Destino | Cuándo |
| --- | --- | --- |
| Monitor del servicio o del generador de carga | Vigía, `POST /api/alerta` | Al detectar un episodio de errores o latencia. Usar el mismo identificador de episodio para sus actualizaciones. |
| Vigía | Puente, `POST /api/ingesta/eventos` | Por cada herramienta ejecutada, al publicar una propuesta, al informar de la acción y su verificación y al cerrar el informe. |
| Vigía | Puente, `GET /api/casos/{caso}/decision?espera=25` | Después de que el puente confirme la recepción de una propuesta de riesgo medio o alto en modo asistido. |
| Servidor de la HU o generador de carga, según acuerdo | Puente, `POST /api/ingesta/metricas` | Desde antes del despliegue hasta después de la recuperación. Elegir un único emisor para toda la demo. |

Configurar la URL base y el token del puente en el proceso emisor. Enviar `X-Vigia-Token` en todas las llamadas al puente. La credencial que recibe el monitor para la API de Vigía es independiente.

## Publicación de herramientas y propuesta

El adaptador de salida puede consumir los eventos que Vigía ya emite. Cada evento saliente debe persistir su ID y contenido antes de enviarse. Reintentar el mismo POST con el mismo ID, preservar el orden dentro del caso y continuar desde la última entrega confirmada si el puente se reinicia. Un timeout puede ocurrir después de que el puente haya guardado un evento.

| Herramienta de Vigía | Nombre esperado en el puente | Contenido para el público |
| --- | --- | --- |
| `query_metrics` | `consultar_metricas` | P95 observado, valor de referencia, unidad y ventana disponibles. |
| `detect_anomaly` | `detectar_anomalia` | `resultado.minutos_para_cruce` contiene los minutos proyectados o `null` si la tendencia es indeterminada. |
| `list_changes` | `listar_cambios` | En `resultado`, identificar la HU o despliegue observado con `id` y `hora`. Conservar la relación entre HU, release y SHA en Vigía. |
| `search_logs` | `buscar_logs` | En `resultado.primera_ocurrencia`, enviar la hora del primer error observado. Compartir la zona horaria usada para marcar el despliegue y los errores. |
| `propose_action` | Evento `tipo: propuesta` | Traducir riesgo y acción al vocabulario del puente. Para rollback, enviar `accion: revertir_cambio`, `objetivo`, justificación y evidencia con texto y fuente. |

Para las demás herramientas, acordar el nombre que se mostrará y emitir un evento `paso` por llamada. Los resúmenes se redactan en español a partir de resultados reales. Exponer solo argumentos y resultados saneados. Registrar fallos de herramientas como tales y conservar la diferencia entre fuente inaccesible y consulta sin resultados. Las cifras y las horas de los ejemplos del contrato son ilustrativas.

En el código, `_audit_and_emit` ya recibe herramienta, argumentos, resumen y referencias. Muchos pasos usan un `payload` vacío. Enriquecer en origen los eventos con el subconjunto estructurado del resultado que necesita el gráfico. Obtener la justificación y las citas de la propuesta persistida y sus evidencias. [Eventos de herramientas](../src/vigia/application/tools.py) · [Publicación local actual](../src/vigia/adapters/outbound/audit.py)

## Decisión humana y acción

1. Asociar el ID de la propuesta y su versión con el caso y el evento publicado. Iniciar el long-poll fuera del hilo que atiende la alerta. Configurar el timeout HTTP por encima de los 25 segundos del puente. Si responde `pendiente`, repetir. Ante un error de transporte, reintentar con espera progresiva.
2. Traducir `aprobar` o `rechazar` a la decisión interna y resolverla mediante el servicio de aprobaciones con una clave de idempotencia estable. Un voto repetido debe producir una sola decisión y, como máximo, una ejecución. Ante rechazo, comunicar el rechazo y el seguimiento real del caso.
3. El contrato suministrado consulta la decisión por **caso**. Si el chat puede reemplazar una propuesta mientras otra pestaña la muestra, acordar que la respuesta identifique la propuesta y versión votadas y al operador. Aplicar una decisión únicamente a esa versión. Para una demo sin esa extensión, limitar el recorrido a una única propuesta decidible por caso. Una acción posterior a una reevaluación necesita una correlación inequívoca.
4. Ante aprobación, comunicar la ejecución cuando el sistema de despliegue la confirme. Emitir un paso de acción, otro con la verificación observada y un mensaje final. Usar `simulada: true` cuando la acción solo afectó al simulador. Si el p95 continúa alto o falta tráfico posterior, comunicar «recuperación pendiente». El ejecutor actual del piloto es simulado. [Servicio de decisiones](../src/vigia/application/approval_service.py) · [Configuración del ejecutor](../src/vigia/runtime.py)

## Métricas y prueba entre repositorios

Se propone que el generador de usuarios mida la latencia de extremo a extremo y publique cada muestra una vez, incluidos errores y timeouts. Si el equipo del servidor ya implementó esta publicación, mantenerlo como único emisor y acordar cómo contabilizar los timeouts. El puente agrupa cada 10 segundos y calcula el p95 de las solicitudes exitosas y el porcentaje de fallos. Las muestras alimentan el gráfico. Vigía necesita además acceso a la fuente de telemetría y al registro que identifica el SHA desplegado para fundamentar su diagnóstico.

**Aceptación entre equipos.** Arrancar el puente con `npm run puente` en el repositorio de la UI y contrastar el contrato con `api/README.md` y `api/agente_simulado.py`. Generar tráfico sano, desplegar la HU, mantener tráfico durante el fallo y observar pasos y marcas temporales. Reenviar un evento tras un timeout y comprobar que aparece una sola vez. Probar aprobación y rechazo en casos separados. Para la aprobación real, verificar el SHA activo y la recuperación medida por nuevas solicitudes. El éxito del POST o el clic del operador solo confirman sus respectivos pasos.

El chat, el botón de investigación manual y el aviso directo de decisión son ampliaciones opcionales. Para conversaciones, el puente puede adaptar el mensaje simple de la UI a la ruta existente de Vigía `POST /api/casos/{case_id}/mensajes` y recuperar la respuesta del turno. [API de Vigía](../src/vigia/adapters/inbound/http_api.py)
