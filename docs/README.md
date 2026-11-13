# Vigía · Agente de prevención e investigación de fallas en producción

**Vigía** es un asistente de ingeniería operativa en Python que investiga señales tempranas de degradación en producción antes de que escalen en un incidente grave, vincula los síntomas con cambios desplegados y dependencias reales, y mantiene la remediación bajo aprobación humana explícita y verificación posterior.

> **Estado actual:** La arquitectura **A (Investigador con herramientas de lectura)** está implementada como paquete Python instalable (`src/vigia/`) con arquitectura hexagonal, contratos en Pydantic v2, ejecución con LLM vía LangChain o modo demo offline, modo piloto (`shadow` / `assisted`), investigación conversacional por cohortes, conectores de solo lectura (Git local, GitHub, Prometheus, Loki, Draw.io), persistencia SQLite y consola web HTTP/SSE.

---

## 1. Qué problema resuelve Vigía

Cuando aparece una anomalía temprana (por ejemplo, un aumento progresivo de latencia p95 en **Transferencias** de `180 ms` a `420 ms` con proyección de alcanzar `800 ms` en `24 minutos`), los equipos suelen perder minutos críticos correlacionando alertas, diagramas, despliegues y logs dispersos.

Vigía automatiza el diagnóstico inicial y responde tres preguntas operativas con evidencia citable:

1. **¿Qué podría fallar y con cuánto margen?** Evalúa la anomalía en la hora de corte (`cutoff`) sin filtrar datos posteriores ni del futuro del incidente.
2. **¿Qué cambio y qué dependencia explican los síntomas?** Sigue la topología (`Transferencias` -> `pagos-core`), resuelve qué versión estaba realmente desplegada en ese entorno (`CHG-4821`), compara revisiones y contrasta hipótesis rivales (regresión del cambio vs. pico de tráfico o fallos previos) usando logs, métricas, incidentes históricos (`INC-3512`, `INC-3307`) y runbooks (`RB-087`).
3. **¿Qué acción merece aprobación y cómo comprobaremos su efecto?** Emite una sola recomendación justificada con citas verificables (`EV-xxx`). Las acciones de riesgo medio o alto esperan autorización humana y el caso solo se cierra si una medición **posterior** a la ejecución confirma la recuperación (por ejemplo, `184 ms`).

---

## 2. Capacidades implementadas actualmente

### Investigación autónoma acotada y trazable
- **Corte temporal estricto (`cutoff`):** las consultas de telemetría, despliegues y arquitectura se acotan al instante de la alerta para evitar contaminar el diagnóstico con eventos futuros.
- **Vinculación verificable con el despliegue activo:** solo atribuye evidencia a revisiones efectivamente desplegadas en el servicio y entorno investigados, descartando commits posteriores no desplegados.
- **Evidencia citable y detección de lagunas:** cada lectura genera evidencias tipadas (`EV-xxx`) con origen, ventana, cobertura y definición de error; si faltan series o existen contradicciones, Vigía las reporta sin fabricar métricas sintéticas.

### Perfiles y modos de ejecución (`demo` / `pilot`, `shadow` / `assisted`)
- **Perfil `demo` / modo `assisted`:** reproduce el flujo completo de investigación, propuesta, pausa para aprobación humana, ejecución simulada idempotente y verificación posterior antes de cerrar el caso (`approve` cierra en `184.0 ms`; `reject` registra un único ticket preventivo y deja el caso abierto).
- **Perfil `pilot` / modo `shadow`:** ejecuta la investigación en **solo lectura**, guarda la recomendación (`recommended` / `awaiting_review`) sin ejecutar escrituras sobre producción y expone métricas de seguimiento del piloto (`GET /api/piloto/metricas`) y sondeo de conectividad por servicio (`--probe-service`).

### Investigación conversacional y reevaluación por cohortes
- Permite consultar el expediente del caso o plantear hipótesis alternativas desde la consola y la API (`GET/POST /api/casos/{case_id}/mensajes`), por ejemplo preguntando si la degradación proviene de un aumento de tráfico o de una versión específica.
- Compara tasas y volúmenes entre ventanas y cohortes (`cohort_comparison.py`), reutilizando únicamente comparaciones concluyentes con clave compatible y evitando bloquear reintentos cuando una comprobación previa fue indeterminada.
- Si una reevaluación mantiene la misma acción propuesta, conserva el `proposal_id` e incrementa `proposal_version`. El modo offline (`_OfflineDemoChatModel`) reproduce también este comportamiento conversacional sin requerir llamadas a un LLM externo.

### Consola web, API HTTP/SSE y garantías de seguridad
- **API HTTP y eventos en tiempo real (`VigiaHttpApp`):** incluye consola web (`GET /`), ingesta de alertas (`POST /api/alerta`), consulta de caso (`GET /api/caso`), flujo SSE (`GET /api/eventos`), revisión en modo sombra (`POST /api/revision`), decisión de aprobación (`POST /api/aprobacion`), métricas piloto (`GET /api/piloto/metricas`), sonda de servicio (`GET /api/servicio/prueba`) y mensajería conversacional (`GET/POST /api/casos/{case_id}/mensajes`).
- **Aprobación ligada a la versión vigente:** `POST /api/aprobacion` exige `proposal_version` explícita para impedir que un operador apruebe una versión anterior tras una reevaluación.
- **Identidad verificada en decisiones:** soporta sesiones firmadas con HMAC (`issue_session_token`) o encabezados emitidos por un proxy autenticado (`trusted_proxy_token`) cuando `require_user_auth` está activo, evitando suplantación directa desde el cliente.
- **Protección contra XSS y concurrencia:** la consola renderiza preguntas, respuestas, hipótesis y evidencias con `textContent`/`createElement` (sin `innerHTML`), y la persistencia SQLite en modo WAL (`case_store.py`) protege los casos con *leases* en `work_queue` y reservas idempotentes (`operation_reservations`).

### Conectores y adaptadores implementados
- **Código y despliegues:** `local_git.py` (lectura Git acotada por SHA/líneas) y `github.py` (GitHub Deployments con resolución de estado mediante `/statuses` y comparación de commits).
- **Arquitectura y catálogo:** `drawio.py` (extracción de relaciones desde XML de Draw.io vinculada a revisión) y `service_catalog.py` (mapeo de alias, selectores y repositorios por servicio).
- **Telemetría:** `telemetry.py` (escenario sintético empaquetado para pruebas/demo) y `http_telemetry.py` (Prometheus con contadores reales de solicitudes/errores y cálculo de cobertura temporal, y Loki con timestamps Unix en nanosegundos).
- **Antecedentes y auditoría:** `knowledge.py` (búsqueda de incidentes y runbooks), `executor.py` (ejecutor simulado y tickets preventivos) y `audit.py` (registro de eventos de auditoría).

---

## 3. Cómo transcurre una investigación de extremo a extremo

| Momento | Qué sucede | Qué produce Vigía |
| --- | --- | --- |
| Alta del servicio | Se registran servicio, alias, repositorio, fuente de despliegues, selectores de telemetría y ruta de diagrama. | Catálogo unificado para correlacionar fuentes con nombres distintos. |
| Despliegue y señal temprana | Llega una anomalía (p95 creciente en `Transferencias`, ventana, línea base y umbral proyectado) junto a la hora de corte. | Caso acotado al servicio, entorno e instante de corte en SQLite (`CASE-xxxx`). |
| Investigación | El agente consulta dependencias (`Draw.io`), resuelve el release activo (`CHG-4821` en `pagos-core`), lee el diff y código, revisa logs/métricas y busca antecedentes. | Evidencias `EV-xxx`, hipótesis a favor/en contra, lagunas y un diagnóstico trazable. |
| Decisión | Vigía emite una propuesta (`propose_action`); el backend calcula su nivel de riesgo (`LOW`, `MEDIUM`, `HIGH`). | En `shadow` queda registrada como recomendación; en `assisted` espera aprobación sobre `proposal_id` + `proposal_version`. |
| Reevaluación (opcional) | El operador consulta o cuestiona la causa vía chat (`POST /api/casos/{case_id}/mensajes`). | Respuesta fundamentada o reevaluación por cohortes que actualiza la versión o propuesta vigente. |
| Verificación | Tras aprobar una acción, el ejecutor actúa y el agente exige una lectura **posterior** a la ejecución; si se rechaza, abre un ticket preventivo único. | Cierre solo si la métrica posterior confirma recuperación; en caso contrario o ante rechazo, el caso permanece abierto. |

---

## 4. Arquitectura e historial de alternativas (A, B y C)

Durante el diseño inicial se evaluaron tres arquitecturas posibles para resolver el mismo caso base (`Transferencias` -> `pagos-core`). **Se eligió e implementó la Alternativa A** por su simplicidad operativa, trazabilidad directa y adecuación al alcance del MVP y piloto.

| Decisión | A · Investigador con herramientas *(Implementada)* | B · Mapa por despliegue y agente *(Diseño futuro)* | C · Supervisor y especialistas *(Diseño futuro)* |
| --- | --- | --- | --- |
| Unidad de investigación | Un caso y un ciclo modelo–herramientas | Un subgrafo versionado de la topología + señales del caso | Subtareas coordinadas por especialidad |
| Lectura de arquitectura | A demanda durante la investigación | Indexada previamente por despliegue | Interpretada bajo demanda por especialista |
| Quién decide el siguiente paso | El investigador elige herramientas según hallazgos | Recorrido del grafo acota candidatos y el agente contrasta | Un supervisor reparte preguntas y sintetiza conflictos |
| Cuándo compensa | MVP, piloto inicial y trazabilidad directa por herramientas | Muchas dependencias compartidas y consultas repetidas de impacto | Fuentes heterogéneas donde el paralelismo justifique el coste |

---

## 5. Estructura actual del repositorio

```text
.
├── pyproject.toml                       # Metadatos del paquete, dependencias y entrypoint CLI `vigia`
├── uv.lock                              # Lockfile reproducible gestionado con uv
├── README.md                            # Guía rápida en inglés para clon limpio, pruebas y wheel
├── src/vigia/
│   ├── __init__.py
│   ├── runtime.py                       # Composición de dependencias y arranque del runtime
│   ├── domain/
│   │   ├── contracts.py                 # Contratos Pydantic (Alert, Evidence, Proposal, CohortComparison, CaseRecord...)
│   │   └── ports.py                     # Puertos hexagonales hacia SCM, telemetría, arquitectura, store y ejecutor
│   ├── application/
│   │   ├── investigator.py              # Orquestador del investigador (Alternativa A)
│   │   ├── tools.py                     # Exposición tipada de puertos como herramientas de lectura y propuesta
│   │   ├── prompts.py                   # Prompts de investigación y síntesis de expediente
│   │   ├── case_service.py              # Ciclo de vida de casos, modos shadow/assisted y consultas
│   │   ├── approval_service.py          # Validación de decisiones, versión de propuesta, idempotencia y verificación
│   │   ├── evidence_registry.py         # Registro y verificación de citas EV-xxx por caso
│   │   ├── cohort_comparison.py         # Comparación estadística/temporal por cohortes y reutilización segura
│   │   └── conversation_service.py      # Flujo conversacional de preguntas y reevaluación en el caso
│   ├── adapters/
│   │   ├── inbound/
│   │   │   └── http_api.py              # Servidor HTTP/SSE y consola web embebida (VigiaHttpApp)
│   │   └── outbound/
│   │       ├── local_git.py             # Lector Git local por SHA y líneas
│   │       ├── github.py                # Conector GitHub Deployments (/statuses) y comparación de commits
│   │       ├── drawio.py                # Lector de diagramas Draw.io XML
│   │       ├── telemetry.py             # Adaptador de telemetría y logs sintéticos
│   │       ├── http_telemetry.py        # Adaptadores reales para Prometheus y Loki
│   │       ├── service_catalog.py       # Catálogo de servicios, alias y selectores
│   │       ├── knowledge.py             # Búsqueda de incidentes y runbooks
│   │       ├── case_store.py            # Persistencia SQLite WAL, leases en work_queue y turnos
│   │       ├── executor.py              # Ejecutor simulado de acciones y tickets preventivos
│   │       └── audit.py                 # Registro de auditoría y eventos
│   ├── entrypoints/
│   │   └── cli.py                       # CLI `vigia` y modelo guionado `_OfflineDemoChatModel`
│   └── demo/                            # Escenario sintético empaquetado dentro del wheel
├── tests/
│   ├── conftest.py
│   ├── test_investigation.py            # Diagnóstico, corte temporal, citas y commits no desplegados
│   ├── test_decisions.py                # Aprobación, rechazo, idempotencia y verificación post-ejecución
│   ├── test_adapters.py                 # GitHub, Prometheus, Loki, Draw.io y conectores locales
│   ├── test_pilot.py                    # Modos shadow/assisted, sondeo, métricas, autenticación y versión HTTP
│   ├── test_conversational.py           # Chat de seguimiento, comparación por cohortes y concurrencia/leases
│   └── test_packaging.py                # Construcción del wheel y ejecución CLI fuera del checkout
└── docs/                                # Documentación funcional, diseños, diagramas y maquetas
```

---

## 6. Comandos principales con `uv`

Requiere Python 3.12+ y [`uv`](https://docs.astral.sh/uv/).

```powershell
# Instalar dependencias de ejecución y desarrollo
uv sync --extra dev

# Ejecutar la suite completa de pruebas (incluye empaquetado de wheel)
uv run pytest -v

# Demo offline (modo asistido): aprobar rollback -> ejecución simulada -> verificación en 184 ms
uv run vigia --demo-offline --decision approve

# Demo offline (modo asistido): rechazar rollback -> ticket preventivo -> caso abierto
uv run vigia --demo-offline --decision reject

# Perfil piloto en modo sombra (solo lectura, sin escrituras externas)
uv run vigia --profile pilot --mode shadow --demo-offline

# Sondeo de conectividad para un servicio configurado
uv run vigia --profile pilot --probe-service --service Transferencias

# Levantar servidor HTTP/SSE y consola web local
uv run vigia --profile pilot --mode shadow --serve --port 8000

# Construcción del wheel distribuible en dist/
uv build --wheel
```

Para ejecutar con un proveedor LLM real, define la clave correspondiente en el entorno (por ejemplo `GOOGLE_API_KEY` o `OPENAI_API_KEY`) y pasa `--model`, por ejemplo:

```powershell
uv run vigia --model google_genai:gemini-2.5-flash --decision approve
```

---

## 7. Mapa de documentación en `docs/`

| Documento | Qué contiene |
| --- | --- |
| [Guía de implementación (Alternativa A)](alternative_a_implementation.md) | Detalle técnico del código implementado, contratos hexagonales, entregas del piloto e investigación conversacional. |
| [Diseño de implementación del piloto](diseno-implementacion-piloto-vigia.md) | Especificación del paso a piloto (modos `shadow`/`assisted`, conectores reales, persistencia y consola). |
| [Diseño de investigación conversacional](diseno-investigacion-conversacional.md) | Diseño del chat en contexto de caso, reevaluación por cohortes y control de versiones de propuesta. |
| [Documento técnico del MVP](Vig%C3%ADa%20Documento%20T%C3%A9cnico.html) | Contexto de arquitectura, contratos de herramientas, escenario de datos y reparto del equipo. |
| [Pitch de Vigía](Pitch%20Vig%C3%ADa.html) · [Versión Markdown](Pitch%20Vig%C3%ADa.md) | Narrativa de presentación del problema, impacto y demo de Vigía. |
| [Maqueta de Consola Vigía](Consola%20Vig%C3%ADa.html) | Prototipo visual HTML de referencia para la consola de guardia. |
| [Diagrama del agente](diagrama-agente-vigia.md) · [Diagrama Draw.io](vigia_mvp_arquitectura.drawio) · [Imagen](arquitectura-agente-vigia.png) | Diagramas visuales y editables de la arquitectura de Vigía. |
| [README raíz del repositorio](../README.md) | Guía rápida en inglés orientada a clon limpio, `uv`, pruebas y paquete distribuible. |
