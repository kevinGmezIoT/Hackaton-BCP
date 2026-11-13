# Diseño de implementación del piloto de Vigía

**Objetivo.** Ante una alerta temprana en Transferencias, el equipo de guardia debe poder abrir la consola y responder tres preguntas con datos del mismo episodio. ¿Cuánto margen queda antes de afectar al cliente? ¿Qué evidencia vincula la demora con un cambio en `pagos-core`? ¿Qué intervención recomienda Vigía y cómo se comprobaría su efecto?

**Primer entregable.** Un piloto en **modo sombra** que lee fuentes operativas reales, investiga y publica recomendaciones revisables. El equipo califica la recomendación. Las intervenciones que realice por sus propios canales se pueden observar y registrar. La aprobación y el despacho de acciones desde Vigía corresponden a una etapa posterior. Los valores de 180, 420, 800 y 184 ms usados aquí pertenecen al escenario sintético del [pitch](<Pitch Vigía.md>).

## 1. Qué se reutiliza y qué se construye

| Capacidad | Punto de partida | Cambio para el piloto |
| --- | --- | --- |
| Investigación | Un agente LangChain con 11 herramientas, límite de llamadas, evidencias por caso y citas verificadas. | Conservar el investigador. Conectar las herramientas a datos del servicio y comprobar la evidencia exigida para recomendar una reversión. |
| Alta y fuentes | Catálogo YAML, dependencias en Draw.io, Git local, despliegues JSON, métricas CSV, logs e incidentes en archivos y guías Markdown. | Reutilizar catálogo, diagrama y guías. Priorizar alerta entrante, telemetría y versión desplegada. |
| Casos y consola | La CLI crea una alerta sintética. Los casos viven en memoria. La consola HTML ilustra el flujo. | Recibir alertas por HTTP, guardar expedientes y conectar la consola a la API de casos y eventos. |
| Decisiones y resultado | El backend calcula riesgo, valida la versión, registra la decisión e invoca un ejecutor simulado. Una muestra posterior favorable puede cerrar el caso. | Separar recomendación, revisión, ejecución confirmada y recuperación sostenida. |

La composición de adaptadores ya está centralizada. Se propone un perfil de piloto junto al perfil de demo para elegir implementaciones en el arranque. El modelo sigue usando los mismos nombres de herramienta. [Composición actual](../src/vigia/runtime.py) · [Puertos de dominio](../src/vigia/domain/ports.py) · [Herramientas](../src/vigia/application/tools.py)

## 2. Alta de Transferencias y sus dependencias

El equipo dueño registra **Transferencias**, donde se observa el p95 de latencia (el tiempo dentro del cual termina el 95 % de las solicitudes), y **pagos-core**, donde se investigan cambios y errores. Cada ficha identifica el servicio en los distintos sistemas. El piloto puede empezar con una modificación revisada del YAML existente.

| Qué registra el equipo | Cómo lo usa Vigía |
| --- | --- |
| Nombre canónico, alias, entorno y equipo responsable | Resuelve la alerta al servicio correcto y controla quién puede consultar o revisar el caso. |
| Repositorio autorizado, fuente de despliegues y referencia de arquitectura | Localiza qué versión estaba activa y qué dependencia comparte el camino de la solicitud. |
| Selectores de métricas y logs, unidad, etiqueta de versión o cohorte cuando exista | Consulta el mismo entorno y compara las instancias afectadas con las demás. |
| Métrica de impacto, umbral, referencia de comportamiento habitual y condiciones de recuperación | Distingue la señal temprana del impacto y define de antemano qué observar después de intervenir. |
| Ubicación de las guías y grupo de aprobadores del servicio objetivo | Recupera el procedimiento aplicable y, en la fase de ejecución, dirige la decisión al dueño de la intervención. |

**Prueba de conexión al registrar el servicio.** Resolver el alias, comprobar la relación declarada `Transferencias → pagos-core` y mostrar al equipo la última muestra con su hora, la versión activa con su revisión y el estado de una consulta de logs. Devolver el resultado por fuente. Una consulta que encontró cero errores, un permiso denegado y una fuente inaccesible requieren respuestas distintas. Conservar el acceso a los datos disponibles y señalar qué conclusiones quedan pendientes. Ampliar los modelos Pydantic para los campos nuevos con valores opcionales o configuración por perfil, de modo que también acepten los catálogos existentes. [Catálogo de ejemplo](../src/vigia/demo/services.yaml) · [Contratos actuales](../src/vigia/domain/contracts.py)

## 3. Adaptadores y sistemas externos

La selección de proveedores queda abierta al inventario del equipo. Grafana, Prometheus, Loki y GitHub ilustran una combinación implementable. Las interfaces del dominio definen qué debe entregar la plataforma elegida.

| Orden | Sistema y adaptador | Contrato de integración |
| --- | --- | --- |
| 1 | **Monitor de alertas → adaptador HTTP entrante** | Autenticar al emisor, desagrupar las alertas si vienen en lote y convertir cada una al contrato del apartado 4. Grafana Alerting puede entregar la notificación por webhook. [Documentación](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/) |
| 1 | **Métricas y logs → `TelemetryPort`** | Leer p95, volumen y errores por servicio, entorno y ventana. Devolver unidad, cobertura, hora y referencia reproducible. Combinar dos clientes si los datos viven en plataformas distintas. El cálculo de anomalía y proyección debe poder reutilizarse con el CSV y con series reales. [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/) · [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/) |
| 1 | **CI/CD y Git → `ReleaseSourcePort` y `CodeRepositoryPort`** | Resolver desde el historial de despliegues qué versión servía tráfico al corte, con entorno, hora, revisión previa y activa, y cohorte cuando aplique. Leer en Git el diff y las líneas exactas de esa revisión. Usar GitHub Deployments si el pipeline publica allí los despliegues efectivos. [API de despliegues](https://docs.github.com/en/rest/deployments/deployments) · [Comparación de commits](https://docs.github.com/en/rest/commits/commits#compare-two-commits) |
| 2 | **Arquitectura y conocimiento → `ArchitecturePort` y `KnowledgePort`** | Usar primero el diagrama versionado, las guías y los antecedentes disponibles. Incorporar el gestor de incidentes del banco para leer causa, resolución e ID, y trazas distribuidas cuando ayuden a corroborar una dependencia. |
| 3 | **Tickets, avisos y remediación → `ActionExecutorPort`** | Habilitar cada acción por servicio y entorno. La creación de tickets devuelve un ID externo. Una reversión se solicita al pipeline autorizado y devuelve un ID de operación cuyo resultado se puede consultar. |

Los adaptadores limitan consultas a servicios, entornos, rutas y ventanas autorizados. Entregan al modelo agregados y ejemplos saneados. Las credenciales se resuelven en el proceso del adaptador. Cada lectura registra su estado, referencia, duración y cobertura cuando aplica. Reintentar fallos transitorios con un límite por caso y mostrar las lagunas que persistan.

## 4. De la señal temprana al expediente

~~~mermaid
flowchart LR
    M[Monitor] -->|Alerta temprana| A[API de Vigía]
    A -->|Caso y trabajo pendiente| B[(Base de casos)]
    B --> W[Trabajador de investigación]
    W -->|Solo lectura| F[Telemetría, despliegues, Git y guías]
    W -->|Evidencia y recomendación| B
    B -->|Estado y eventos| A
    A --> U[Consola del responsable]
    U -->|Revisión del caso| A
~~~

**Entrada normalizada propuesta.** El adaptador construye este sobre a partir del formato del monitor. `alert` reutiliza el contrato actual. Los identificadores externos viven en el sobre.

~~~json
{
  "source": "monitor-transferencias",
  "event_id": "evt-091204",
  "episode_id": "p95-transferencias-20261112-0912",
  "state": "firing",
  "alert": {
    "service": "transferencias",
    "environment": "production",
    "metric": "latency_p95_ms",
    "observed_at": "2026-11-12T09:12:04-05:00",
    "current_value": 420,
    "threshold_value": 800
  }
}
~~~

1. **Recepción.** Normalizar fechas con zona horaria, validar emisor, catálogo y entorno. Guardar evento, caso y trabajo pendiente en una transacción. Responder `202` con el `case_id` después de confirmar esa escritura. La investigación continúa en segundo plano.
2. **Correlación.** Una clave única por `(source, event_id)` absorbe reenvíos. Para el episodio, usar fuente, servicio, entorno e identificador de episodio. Si el monitor carece de ID de episodio, derivarlo de la huella de la regla y la hora de inicio. Una muestra nueva se agrega al caso. Un episodio posterior abre otro. Una señal `resolved` describe el estado del monitor y queda en la cronología.
3. **Tiempo de investigación.** Cada investigación fija su hora de corte. Una actualización posterior conserva el corte y la evidencia del diagnóstico anterior. Si amerita reevaluación, abrir una nueva revisión dentro del mismo caso. Consultar solo datos disponibles al corte de esa revisión, incluido el historial de incidentes utilizado como precedente.
4. **Publicación.** Guardar cada evidencia y evento antes de entregarlo a la UI. Un trabajador que reinicia retoma el trabajo desde el último paso confirmado. La restricción de unicidad del episodio y el control de versión del caso evitan que dos trabajadores publiquen decisiones incompatibles.

## 5. Qué debe decidir el agente

**Detección.** El monitor activa el flujo ante la desviación temprana. Vigía consulta la serie para confirmar que la señal tiene volumen y cobertura suficientes. La herramienta calcula el margen hasta el umbral a partir de la tendencia observada y devuelve su ventana. Si los datos permiten confirmar la demora pero la tendencia es inestable, la UI muestra el p95 observado y deja la proyección como indeterminada.

**Diagnóstico.** Para el caso del pitch, el agente sigue la dependencia hacia `pagos-core`, resuelve el despliegue vigente al corte y compara sus revisiones. Después comprueba si los errores de agotamiento de conexiones empezaron tras el cambio, si se concentran en instancias de la nueva versión y si un incremento de tráfico ofrece otra explicación. Puede elegir qué consulta hacer primero. El resultado separa los hechos citados, la causa probable, la evidencia en contra y los datos que faltan.

**Cambio en las herramientas de consulta.** Hoy la búsqueda de logs usa una ventana que termina al corte del caso. Añadir límites de inicio y fin opcionales para comparar intervalos de igual duración antes y después del despliegue, ambos anteriores o iguales al corte. Cuando el tráfico difiera entre versiones, comparar errores por solicitud y conservar los denominadores. Una cohorte con cero solicitudes queda sin cobertura para esa prueba.

**Condición para recomendar una reversión.** Cada herramienta registra en la evidencia el tipo de observación, servicio, ventana, revisión o cohorte y resultado de la comparación. Antes de registrar la propuesta, el backend comprueba que las citas pertenecen al caso, que la versión estaba activa en el objetivo y que hay evidencia de su relación con el síntoma. También exige una comprobación que pudiera refutar la hipótesis y cuyo resultado la respalde. Un antecedente similar orienta esa prueba. Si aparecen errores anteriores al despliegue o un vacío decisivo de telemetría, publicar el hallazgo parcial y la siguiente comprobación. [Prompt actual](../src/vigia/application/prompts.py) · [Registro de evidencia](../src/vigia/application/evidence_registry.py) · [Validación de propuestas](../src/vigia/application/approval_service.py)

**Separación entre recomendar y ejecutar.** Hoy registrar una acción de riesgo bajo puede ejecutarla inmediatamente y rechazar una propuesta puede crear un ticket. En sombra, todas las propuestas terminan en una recomendación persistida. Un operador puede marcarla útil, incorrecta o indeterminada, con comentario. Reservar la autorización de ejecutar para el modo asistido. Aplicar el modo en el backend a todos los caminos de escritura, incluido el rechazo. [Flujo de decisiones actual](../src/vigia/application/approval_service.py)

## 6. Persistencia, API y experiencia en consola

**Persistencia propuesta.** Implementar un `CaseStorePort` con una base transaccional, por ejemplo PostgreSQL. Guardar casos con versión, eventos recibidos, evidencias, propuestas, revisiones humanas y operaciones externas. El expediente conserva la versión de configuración del servicio, el modo operativo y la hora de corte. Usar una secuencia de eventos por caso para alimentar la cronología. Guardar el cambio de estado, su evento de auditoría y cualquier trabajo pendiente en la misma transacción. El archivo JSONL sigue siendo útil para la demo. [Servicio de casos actual](../src/vigia/application/case_service.py)

**Contrato HTTP propuesto.** Las rutas siguen los nombres del documento técnico. El adaptador de entrada traduce el formato de cada proveedor. La API obtiene la identidad humana de la sesión corporativa y valida permisos de lectura por caso y de aprobación sobre el servicio objetivo. [Contrato de referencia del MVP](<Vigía Documento Técnico.html>)

| Operación | Resultado que consume el cliente |
| --- | --- |
| `POST /api/alerta` | Devuelve el caso y si el evento fue nuevo, agregado al episodio o repetido. Una entrega repetida devuelve el mismo caso. |
| `GET /api/caso?caso=…` | Devuelve servicio, entorno, modo, estado, gráfica o referencia de métricas, fuentes consultadas, hallazgo, propuesta, decisión y verificación. |
| `GET /api/eventos?caso=…` | Publica eventos por SSE con ID secuencial. Al reconectar, el cliente envía el último ID recibido para recuperar los siguientes. |
| `POST /api/revision` | En sombra, registra la evaluación del equipo y la revisión de la propuesta a la que corresponde. Alimenta las métricas del piloto. |
| `POST /api/aprobacion` | En modo asistido, recibe caso, propuesta, decisión, comentario y clave de idempotencia. Comprueba autorizaciones y versión de la propuesta. Devuelve `403` sin permiso y `409` ante una decisión conflictiva o un objetivo obsoleto. |

La consola presenta el **síntoma en Transferencias** junto a la **causa probable en pagos-core**. La tarjeta muestra tendencia, impacto estimado, evidencias consultables, lagunas, acción y riesgo. En sombra el botón dice «Evaluar recomendación». En modo asistido se muestran «Aprobar» y «Rechazar» a quien corresponda. Tras decidir, la cronología diferencia quién autorizó, qué confirmó el sistema de despliegue y qué se observó en el servicio.

## 7. Ejecución y verificación en modo asistido

1. Al aprobar, volver a consultar la versión activa con la hora real de la decisión. Si otro despliegue sustituyó al objetivo, devolver el conflicto y solicitar una nueva evaluación. Persistir decisión y orden de ejecución juntas. La misma clave y contenido devuelven el resultado previo.
2. El trabajador despacha al ejecutor del servicio con una clave de operación estable. Guardar el ID del pipeline y esperar confirmación de éxito, error o estado pendiente. Ante un timeout con resultado desconocido, consultar esa operación antes de repetir. Una operación fallida deja el caso abierto con el motivo.
3. Programar la verificación desde la **hora efectiva de finalización** que confirmó el ejecutor. Consultar solo muestras posteriores ya disponibles. El verificador puede reanudarse ante datos tardíos. Usar el servicio que presentó el síntoma para medir la recuperación y la dependencia para comprobar el descenso de errores.
4. Acordar con el dueño la banda saludable de p95, el volumen mínimo, la cobertura, las ventanas consecutivas y la tolerancia de errores. Como prueba de aceptación, una caída aislada de **420 a 410 ms** con umbral de 800 ms y referencia de 180 ms deja el caso en observación. Una serie sostenida cerca de 184 ms, con tráfico suficiente y recuperación de errores, permite cerrarlo. Un ticket creado se registra como seguimiento. Por sí solo no acredita recuperación. [Verificación actual](../src/vigia/application/case_service.py)

## 8. Secuencia de implementación y aceptación

| Entrega | Cambios en el repositorio | Prueba de salida |
| --- | --- | --- |
| A. Alta y fuentes | Ampliar configuración y contratos en `domain/`. Incorporar clientes de telemetría, CI/CD y Git en `adapters/outbound/`. Seleccionar perfiles en `runtime.py`. | Para los dos servicios, reproducir una consulta por entorno y hora. Un commit posterior o aún sin desplegar queda fuera de la explicación. El perfil de demo sigue pasando sus pruebas. |
| B. Alerta y caso durable | Añadir entrada HTTP en `adapters/inbound/`, almacenamiento transaccional y trabajador. Adaptar `CaseService` a la persistencia. | Dos entregas simultáneas del mismo evento y un reinicio producen un expediente, con sus evidencias recuperables. Una segunda alerta del episodio actualiza ese expediente. |
| C. Investigación y consola en sombra | Añadir las comprobaciones a herramientas y propuestas. Conectar consulta de caso, SSE y revisión humana a la consola. | Recorrer una alerta de punta a punta con fuentes de lectura. Probar también un despliegue inocente con errores previos y una fuente denegada. La UI explica la incertidumbre y se registran cero escrituras en sistemas externos. |
| D. Decisión, ejecución y recuperación | Incorporar permisos por servicio, despacho persistido, adaptador del pipeline y verificación programada. Validar primero en entorno de prueba. | Dos aprobaciones concurrentes producen una operación. Una versión reemplazada bloquea la ejecución. Un timeout se reconcilia. Una sola muestra favorable o una ventana sin tráfico mantienen el caso abierto. |

**Cómo evaluar el valor.** Registrar por episodio cuándo llegó la alerta, cuándo apareció la primera recomendación útil, si el p95 cruzó el umbral y qué causa confirmó el equipo. Comparar el tiempo hasta una recomendación útil con el tiempo de diagnóstico de incidentes comparables. Reportar las falsas alarmas sobre episodios evaluados y las causas confirmadas sobre diagnósticos revisados, indicando cuántos quedaron pendientes. Para episodios que cruzaron el umbral, medir el margen observado entre alerta y cruce. Para los demás, conservar la proyección con su hora y registrar si hubo intervención. Al habilitar acciones, contrastar la tasa de episodios con impacto al cliente con la línea base del servicio durante periodos de tráfico comparable.

**Decisiones para iniciar la entrega A.** El equipo de Transferencias identifica las plataformas reales de monitoreo, logs y despliegue, la persona dueña de cada acceso y la consulta que representa su p95 en producción. El equipo de pagos-core confirma cómo vincular una instancia con su versión desplegada. Con esas respuestas se pueden implementar los primeros adaptadores y ensayar el mismo incidente de extremo a extremo.
