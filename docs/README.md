# Vigía · Diseño del agente de prevención de fallas en producción

**Propuesta de construcción para AI Engineer 1 · Paquete Python · Caso inicial de Transferencias**

Vigía recibe una señal temprana y responde tres preguntas para el líder del servicio. ¿Qué podría fallar y con cuánto margen? ¿Qué cambio y qué dependencia explican los síntomas? ¿Qué acción merece aprobación y cómo comprobaremos su efecto? Para responder necesita relacionar el código que llegó a producción, la arquitectura de esa versión y la telemetría del mismo entorno e intervalo.

**Decisión propuesta.** Para la hackatón construiría **A, un investigador con herramientas**. Para un piloto con varios servicios dependientes elegiría **B, un mapa de arquitectura por despliegue y un agente de diagnóstico**. Elegiría **C, un supervisor con especialistas**, si el equipo ya tiene conectores fiables y los ensayos muestran que investigar varias hipótesis en paralelo mejora el diagnóstico. Las tres opciones resuelven el mismo caso y se pueden construir por separado.

El pitch aporta el escenario, la aprobación humana y la ejecución simulada. La lectura del repositorio desplegado, la interpretación de IaC o diagramas y la opción multiagente amplían ese alcance. Los formatos y proveedores que aparecen abajo son decisiones propuestas. [Pitch de Vigía](Pitch%20Vig%C3%ADa.html#demo) · [Documento técnico del MVP](Vig%C3%ADa%20Documento%20T%C3%A9cnico.html#herramientas)

## 1. Cómo transcurre una investigación

| Momento | Qué sucede | Qué obtiene el agente |
| --- | --- | --- |
| Alta de la aplicación | El equipo registra el servicio, sus alias en producción, el repositorio, el origen de despliegues, los selectores de telemetría y las rutas de IaC o diagramas. | Una configuración que permite buscar el mismo componente en fuentes con nombres distintos. |
| Despliegue | CI/CD informa del entorno, hora, versión anterior y nueva, SHA o digest del artefacto y revisión de infraestructura si vive en otro repositorio. Durante un canario puede haber dos versiones activas. | Una referencia verificable por instancia, revisión o cohorte. Permite comparar con la última versión sana. |
| Señal temprana | El monitoreo o el frente de analítica entrega una anomalía, por ejemplo un p95 creciente, con ventana, línea base, umbral y proyección si está disponible. En la demo se reproduce esta entrada desde un archivo. | Un caso acotado a un servicio, entorno y hora de corte. Una nueva muestra del mismo episodio actualiza el caso abierto. |
| Investigación | Vigía localiza dependencias, compara revisiones desplegadas, lee fragmentos de código y consulta errores antes y después del cambio. Busca antecedentes y un procedimiento aplicable. | Una hipótesis con referencias, evidencia que la debilita y datos pendientes. La secuencia depende de lo que encuentre. |
| Decisión | Presenta síntoma, impacto proyectado, causa probable, evidencia y una acción concreta. El backend calcula el riesgo. Una acción media o alta espera una decisión humana asociada a esa propuesta. | Una recomendación que el responsable puede revisar y aceptar o rechazar. |
| Verificación | Ante aprobación, el backend ejecuta la acción simulada. El agente pide una lectura posterior del mismo servicio y entorno. Ante rechazo, se registra un ticket preventivo y el caso sigue abierto. | Una traza que distingue la autorización, el efecto y la recuperación observada. |

**Antes y después del despliegue.** Un cambio de código o IaC también puede iniciar una evaluación previa. Por ejemplo, si una PR reduce un límite de conexiones en una dependencia compartida, Vigía puede señalar qué consumidores estarían expuestos y qué métrica vigilar tras desplegar. Ese informe expresa un riesgo condicionado a que el cambio llegue a producción. El caso del pitch empieza con una señal observada después del despliegue y todavía antes del umbral de impacto. La demo de cuatro horas cubre este segundo recorrido.

### Ejemplo que deben resolver las tres alternativas

A las 09:12, Transferencias pasó de 180 a 420 ms de p95 en 15 minutos. Analítica proyecta que, si continúa esa tendencia, alcanzará 800 ms en unos 24 minutos. El agente debe averiguar por qué revisaría **pagos-core** cuando la alerta nombra **Transferencias**.

1. Encuentra una relación entre ambos servicios en una fuente de arquitectura y conserva la referencia. Si existen trazas, busca una llamada observada en la misma dirección.
2. Resuelve el despliegue de pagos-core de las 08:55 para CHG-4821. Compara con la versión anterior y localiza la configuración que redujo el pool de 80 a 50. En el repositorio de ejemplo se debe poder seguir esa clave hasta el código que crea el pool y citar el archivo y las líneas al SHA correcto.
3. Encuentra 312 errores de pool agotado desde las 09:01. Comprueba si ya ocurrían antes del cambio y, si existe la dimensión de versión, si se concentran en la cohorte nueva. Un pico de tráfico concurrente sería otra explicación que conviene contrastar.
4. Relaciona el patrón con INC-3512, INC-3307 y el procedimiento RB-087. Propone revertir CHG-4821, explica por qué y espera la decisión. Después de la aprobación, el escenario sintético devuelve 184 ms a las 09:19. Esa lectura permite confirmar la recuperación.

Las cifras y los identificadores proceden de los documentos del proyecto. El vínculo verificable entre despliegue, archivo, líneas y dependencia es el nuevo entregable. En el piloto, una contradicción o una fuente ausente debe figurar en el informe y puede justificar mantener abierta la investigación. [Datos del escenario](Vig%C3%ADa%20Documento%20T%C3%A9cnico.html#datos)

## 2. Cómo se conectan las fuentes

| Fuente | Implementación propuesta | Evidencia que devuelve |
| --- | --- | --- |
| Repositorio y despliegues | Para el MVP, repositorio Git de ejemplo con dos commits y manifiesto de release. Leer con operaciones Git acotadas como `git diff <base> <sha> -- <ruta>` y `git show <sha>:<ruta>`. Para el piloto, un adaptador de solo lectura al SCM y otro al historial de CI/CD. | Repositorio, SHA, ruta, líneas y fragmento mínimo. El manifiesto vincula servicio y entorno con ese SHA. Un parche truncado obliga a leer las dos versiones del archivo. |
| Logs, métricas y trazas | Para la demo, registros sintéticos fechados. En piloto, consultar el backend de observabilidad ya implantado. Filtrar por servicio, entorno y ventana, y por versión o instancia cuando exista. Pedir conteos y ejemplos redactados. Correlacionar por identificador de traza cuando esté disponible. | Ventana con zona horaria, consulta reproducible, conteo, muestra, cobertura y referencia. Analítica entrega anomalía, proyección y umbral como datos de herramienta. |
| Infraestructura como código | Leer los manifiestos o artefactos aprobados del despliegue. Para Terraform, extraer recursos, referencias y cambios de un plan JSON previamente saneado. Para Kubernetes, relacionar selectores, aplicaciones, configuración y referencias a servicios en los manifiestos efectivos. | Recurso, atributo, valor anterior y nuevo si procede, revisión y origen. Una dependencia de aprovisionamiento y una llamada entre servicios se etiquetan como relaciones diferentes. |
| Diagramas de arquitectura | Empezar con Draw.io XML exportado sin compresión. Extraer nodos, etiquetas, extremos y dirección de las flechas. Resolver alias con el catálogo del servicio. Si solo hay una imagen, una extracción visual puede proponer relaciones que se corroboran con IaC, código o trazas. | Por relación, origen, destino, tipo, versión, página o identificador de celda y estado de corroboración. Si la flecha carece de dirección o significado, conservarla como ambigua. |
| Incidentes y runbooks | Invocar las herramientas de búsqueda del equipo. Recuperar incidentes parecidos y las condiciones de aplicación del procedimiento. | Identificador, fuente, causa y solución anteriores. Un antecedente ayuda a elegir una comprobación o una acción. |

**Identidad y vigencia.** Cada lectura comparte servicio, entorno, intervalo y, cuando la fuente lo permite, versión desplegada. Con despliegues parciales se conserva la cohorte. Si CI/CD solo aporta un tag mutable, resolverlo mediante la procedencia del artefacto. Si no se puede identificar la revisión activa, informar del síntoma y de esa laguna. La coincidencia en el tiempo por sí sola es insuficiente para recomendar revertir un commit concreto. El mapa conserva por separado la arquitectura declarada y las llamadas observadas. Una traza ausente también puede reflejar muestreo.

**Límite de acceso.** El adaptador autoriza repositorios, rutas, entornos y ventanas antes de consultar. Entrega al modelo solo fragmentos y campos permitidos. El código, los logs, los diagramas y los runbooks se procesan como datos. Los secretos de conexión permanecen en el adaptador. En particular, un plan o estado Terraform convertido a JSON puede exponer valores sensibles en claro. La extracción debe seleccionar campos permitidos antes de guardar evidencias o llamar al modelo. [Formato JSON de Terraform](https://developer.hashicorp.com/terraform/internals/json-format) · [Advertencia de Terraform sobre valores sensibles](https://developer.hashicorp.com/terraform/cli/commands/show)

Para una instalación con GitHub y Loki, el conector puede comparar commits y consultar logs por rango temporal. OpenTelemetry da un modelo de registros para conservar fecha del evento e identificadores de traza opcionales. El equipo puede elegir otros conectores detrás de los mismos contratos. [Comparación de commits en GitHub](https://docs.github.com/en/rest/commits/commits#compare-two-commits) · [Consultas por rango en Loki](https://grafana.com/docs/loki/latest/reference/loki-http-api/) · [Modelo de logs de OpenTelemetry](https://opentelemetry.io/docs/specs/otel/logs/data-model/) · [Fuente XML de Draw.io](https://www.drawio.com/doc/faq/diagram-source-edit)

## 3. Tres arquitecturas implementables

| Decisión | A · Investigador con herramientas | B · Mapa por despliegue y agente | C · Supervisor y especialistas |
| --- | --- | --- | --- |
| Unidad de investigación | Un caso y un historial del modelo | Un subgrafo de la versión afectada más las señales del caso | Una investigación coordinada con tareas acotadas por especialidad |
| Cuándo lee la arquitectura | A demanda durante el caso | Al registrar una versión, antes de la alerta | El especialista la interpreta al recibir una pregunta |
| Quién elige el siguiente paso | Un modelo elige herramientas según sus resultados | El recorrido del grafo acota candidatos. El agente decide qué comprobar. | El supervisor distribuye preguntas y contrasta los hallazgos |
| Coste dominante | Rondas sucesivas del modelo y lecturas repetidas entre casos | Construcción, actualización y validación del mapa | Llamadas concurrentes, intercambio de hallazgos y síntesis |
| Cuándo compensa | Un servicio y pocos cambios candidatos | Dependencias compartidas y preguntas repetidas de impacto | Fuentes heterogéneas e hipótesis independientes que ameritan trabajo paralelo |

### A. Un investigador con herramientas de lectura

**Flujo.** Un agente recibe la alerta con la configuración del servicio y un catálogo tipado. Si el síntoma aparece en Transferencias, puede consultar la dependencia, resolver el release de pagos-core, inspeccionar el diff y luego elegir una búsqueda de logs que pruebe su hipótesis. Cada llamada devuelve hechos citables y un indicador de cobertura. Cuando tiene suficiente respaldo invoca `proponer_accion`. El backend guarda la propuesta y decide si pausar. Un mensaje del modelo por sí solo carece de efecto sobre producción.

**Stack y código propio.** Python, `langchain.agents.create_agent` para el ciclo modelo–herramienta y Pydantic para validar entradas y salidas. En `estrategias/investigador.py` se registran las herramientas habilitadas e instrucciones de investigación. El contexto del modelo contiene un resumen y referencias de evidencia. El registro completo queda en el caso. El adaptador del modelo usa el proveedor habilitado por la organización. [Agentes y herramientas en LangChain](https://docs.langchain.com/oss/python/langchain/agents) · [Modelos de Pydantic](https://docs.pydantic.dev/latest/concepts/models/)

**Orden de construcción.** Primero resolver el release y leer un diff reproducible. Después incorporar telemetría y una relación verificable de arquitectura. Finalmente conectar la propuesta, la aprobación y la verificación. El riesgo de diseño es que el investigador se aferre al cambio más reciente. El primer ensayo debe incluir un commit posterior nunca desplegado y comprobar cuál cita. Si agota las rondas, emite los hallazgos parciales y la siguiente comprobación sugerida.

### B. Mapa de dependencias versionado y agente de diagnóstico

**Flujo.** Al recibir un despliegue, un indexador construye una **instantánea de la topología** con nodos de servicio, versión, recurso y archivo. Las aristas, como `llama_a`, `configura` y `desplegado_desde`, guardan procedencia. Una alerta inicia un recorrido acotado por entorno, tiempo, tipo de relación y número de saltos. Para Transferencias, el recorrido localiza pagos-core y los archivos o recursos que cambiaron en su release. El agente recibe ese subgrafo, contrasta candidatos con telemetría y puede pedir evidencia adicional. Para una PR, el recorrido inverso enumera consumidores potencialmente afectados.

**Stack y código propio.** NetworkX para recorrer el grafo dirigido y SQLite para conservar instantáneas, relaciones y evidencias del piloto. `estrategias/topologia/indexar.py` publica cada versión completa en una transacción. `consultar.py` reconstruye el subgrafo correspondiente al instante de la alerta, y `diagnosticar.py` usa un agente con herramientas para contrastarlo con el comportamiento observado. El grafo representa componentes del sistema analizado. La aprobación sigue el contrato común. [Grafos dirigidos en NetworkX](https://networkx.org/documentation/stable/reference/classes/digraph.html)

**Orden de construcción.** Primero extraer y comprobar a mano el camino Transferencias → pagos-core en una versión. Después incorporar una segunda versión con una dependencia distinta y probar consultas históricas. Finalmente incorporar el agente y probar una alerta tras un cambio compartido. Un mapa incompleto puede ocultar la causa real. Si una instancia desplegada carece de instantánea válida o la evidencia contradice el mapa, el investigador consulta fuentes directas y señala el conflicto. El coste de mantener el índice se justifica cuando varias alertas reutilizan la misma arquitectura.

### C. Supervisor con especialistas de código, telemetría y arquitectura

**Flujo.** Un supervisor abre el caso y fija el corte temporal, entorno y versiones. Distribuye preguntas concretas. El especialista de **código y releases** inspecciona qué cambió y dónde se usa esa configuración. El de **telemetría** busca cuándo aparecen síntomas, qué cohortes afectan y qué señales los contradicen. El de **arquitectura** verifica si existe un camino entre el componente modificado y el afectado. Cada uno decide qué herramientas de lectura de su dominio invocar. Devuelve hallazgo, referencias, lagunas y una prueba que podría refutarlo. Solo el supervisor puede emitir la propuesta.

**Stack y código propio.** LangGraph `StateGraph` para coordinar ramas, reunir resultados y reanudar la ejecución. En `estrategias/equipo/`, cada especialista tiene un prompt y herramientas acotadas. `supervisor.py` asigna un identificador por tarea, reúne hallazgos por referencia y resuelve discrepancias pidiendo una comprobación dirigida. Se fija un presupuesto total de rondas y tiempo. Si una rama falla se conservan los resultados válidos. Una pausa con `interrupt` requiere un *checkpointer* y el mismo `thread_id` para reanudar. Los efectos externos deben deduplicarse porque al reanudar puede repetirse parte del nodo. [Patrones de orquestación en LangGraph](https://docs.langchain.com/oss/python/langgraph/workflows-agents) · [Pausa y reanudación](https://docs.langchain.com/oss/python/langgraph/interrupts)

**Orden de construcción.** Primero acordar un formato de hallazgos y ejecutar cada especialista sobre el mismo caso. Después conectar dos ramas paralelas y añadir la de arquitectura. Por último probar desacuerdo, rama ausente y decisión humana. Por ejemplo, si código identifica una reducción de pool pero telemetría encuentra errores anteriores al despliegue, el supervisor debe buscar otra causa o explicar la incertidumbre. Tres especialistas que citan el mismo log aportan una sola fuente. Medir si el paralelismo mejora el diagnóstico antes de asumir el mayor coste por caso.

## 4. Estructura del repositorio del agente

Árbol **propuesto por construir**. Implementar el directorio de la alternativa elegida. El repositorio de la aplicación analizada es un insumo externo, igual que el servicio de logs.

```text
app/
  pyproject.toml                     # Dependencias del paquete y de la estrategia elegida
  src/vigia/
    contratos.py                     # Alerta, Release, Dependencia, Evidencia, Hallazgo, Propuesta
    puertos.py                       # Interfaces tipadas a fuentes, analítica, auditoría y ejecutor
    casos.py                         # Iniciar caso, consultas informativas y decisión humana
    evidencia.py                     # Índice por caso, citas, fecha y origen de los resultados
    aprobaciones.py                  # Correlación de propuesta, riesgo y decisión autenticada
    herramientas.py                  # Expone los puertos de lectura al modelo y valida argumentos
    estrategias/
      investigador.py                # A · agente con herramientas
      topologia/                     # B · indexar.py, consultar.py, diagnosticar.py
      equipo/                        # C · supervisor.py, codigo.py, telemetria.py, arquitectura.py
    prompts/                         # Instrucciones y forma del informe para la estrategia elegida
    cli.py                           # Reproducir un caso e introducir la decisión por terminal
  tests/
    fixtures/transferencias/         # Dos revisiones, release, IaC o diagrama, señal y logs sintéticos
    test_investigacion.py            # Cambio desplegado, evidencia adversa y citas
    test_decisiones.py               # Aprobación, rechazo, duplicado y verificación
```

**Responsabilidades.** AI Engineer 1 es dueño de los contratos que consume, la orquestación, los prompts, el contexto, la pausa y los eventos. AI Engineer 2 aporta los adaptadores de herramientas y la auditoría. Analítica aporta mediciones, anomalías, proyecciones y recuperación de incidentes similares. El LT conecta la API, el origen autenticado de decisiones y el ejecutor simulado. Para desarrollar desde el primer día, AI Engineer 1 inyecta implementaciones de prueba de esos puertos. En B, el índice de dependencias es contexto operativo del agente. El cálculo de anomalías sigue perteneciendo a analítica. [Reparto del equipo en el documento técnico](Vig%C3%ADa%20Documento%20T%C3%A9cnico.html#equipo)

### Contratos para trabajar en paralelo

Ejemplo de alta para el ensayo. Los nombres de repositorios, rutas y selectores son ilustrativos. El diagrama de entrada debe describir la aplicación que se investiga. El archivo Draw.io que acompaña al pitch describe la arquitectura de Vigía.

```yaml
servicio: transferencias
entorno: produccion
repositorio: aplicaciones/transferencias
fuente_releases: fixtures/transferencias/despliegues.json
arquitectura:
  ruta: docs/transferencias.drawio
  revision: desde_release
telemetria:
  selector_servicio: transferencias-api
  selector_entorno: prod
```

El catálogo incluye otra entrada para pagos-core con su propio repositorio y selector. Si la relación sale del diagrama de Transferencias, el agente conserva la revisión de ese diagrama y resuelve por separado la revisión desplegada de pagos-core para leer su código.

| Interfaz | Entrada → salida | Quién la usa |
| --- | --- | --- |
| `resolver_release`, `comparar_revisiones`, `leer_fragmento` | Servicio, entorno e instante → releases activos. Repo y dos SHA → archivos y cambios. SHA, ruta y líneas → fragmento citable. | Investigador o especialista de código |
| `consultar_dependencias` | Servicio, entorno, versión y dirección → relaciones tipadas con referencias y vigencia | Investigador, índice de B o especialista de arquitectura |
| `buscar_logs`, `consultar_metricas`, `detectar_anomalia` | Servicio, entorno, ventana y filtros → hechos observados, cobertura, anomalía y proyección opcional | Investigador o especialista de telemetría |
| `listar_cambios`, `buscar_incidentes_similares`, `buscar_runbook` | Servicio o descripción → candidatos, antecedentes y pasos referenciados | Investigación y contraste de hipótesis |
| `proponer_accion` | Acción, objetivo desplegado, justificación e identificadores de evidencias del caso → propuesta registrada y riesgo asignado por el backend | Investigador de A/B o supervisor de C |
| `iniciar_caso`, `responder_consulta`, `resolver_aprobacion` | Alerta → ID. Pregunta → respuesta con fuentes. Caso, propuesta, actor y decisión → estado o conflicto. | CLI de ensayo y adaptador API del LT |

**Datos mínimos.** Una `Evidencia` conserva ID, servicio, entorno, hora de observación, fuente, referencia reproducible y hecho observado. Incluye revisión o ventana según la fuente. Un `Hallazgo` conserva hipótesis, evidencias a favor y en contra, lagunas y servicios expuestos. Una `Propuesta` referencia ese hallazgo, el release objetivo, la acción y cómo verificarla. El agente valida que las citas existan en el expediente. Una consulta sin resultados y un conector caído se representan con estados distintos.

**Instrucciones para el investigador.** Identifica el componente que emite el síntoma y el componente que podría causarlo. Para cada hipótesis, elige una lectura capaz de refutarla. Si el error ya existía con la versión anterior, investiga la carga o una dependencia compartida. Si aparece tras el despliegue y se concentra en la versión nueva, sigue el cambio hasta el código o la configuración y contrástalo con la arquitectura. Cita la observación que sustenta cada afirmación, identifica lo que sigues infiriendo y devuelve una sola propuesta con su verificación prevista.

**Decisiones y eventos.** Registrar la propuesta antes de emitirla. Los eventos `paso`, `propuesta`, `decision` y `mensaje` incluyen caso, hora y referencias. Un paso conserva herramienta, argumentos saneados y resumen. El backend aplica la tabla del MVP, con ticket y notificación de riesgo bajo, escalado de capacidad medio y rollback alto. Al aprobar, comprobar que la propuesta sigue pendiente y su release sigue activo. Usar una clave estable por propuesta y acción frente a reintentos. Al rechazar, registrar una única decisión y un único ticket preventivo. Al verificar, consultar una ventana posterior a la ejecución. Si falta la métrica o sigue degradada, el caso queda abierto. La API del LT puede transportar los eventos por SSE. [Contrato del MVP](Vig%C3%ADa%20Documento%20T%C3%A9cnico.html#contrato)

## 5. Etapas de implementación y criterio de salida

| Etapa | Trabajo concreto | Resultado que permite avanzar |
| --- | --- | --- |
| 0:00–0:20 · Contrato | Acordar un servicio, alias, fuente de releases, una representación arquitectónica y el formato de evidencia. Probar acceso a un modelo con herramientas. | Una alerta y una respuesta real de herramienta aceptadas por el paquete. |
| 0:20–1:45 · Primer recorrido A | Preparar dos commits, manifiesto de release y un diagrama o manifiesto simple. Conectar lectura Git local y adaptadores de telemetría sintética. Implementar investigación desde terminal. | El modelo elige al menos cuatro llamadas y llega desde Transferencias a la línea modificada de pagos-core citando SHA, dependencia y ventana de errores. |
| 1:45–2:45 · Integración | Reemplazar dobles por herramientas del equipo, emitir propuesta y reanudar con el ejecutor simulado. | Se observan espera, aprobación y nueva medición. El rechazo produce el ticket. |
| 2:45–4:00 · Validación y demo | Probar un cambio irrelevante, repetir decisiones y ensayar ambos desenlaces. Congelar al minuto 195 según el plan del equipo. | Tres ensayos estables con traza exportable. El cambio irrelevante no aparece como causa. |
| Piloto · Un servicio en modo sombra | Conectar un SCM, CI/CD y el almacén real de logs con permiso de lectura. Acordar selectores, credenciales, ventanas y retención con los responsables. Reproducir incidentes pasados con corte temporal. | El equipo de guardia puede abrir cada referencia y evaluar causas y anticipación sobre casos conocidos. |
| Ampliación · B o C según evidencia | Para B, medir la cobertura del mapa al incorporar un segundo consumidor. Para C, comparar en los mismos casos si los especialistas resuelven más investigaciones contradictorias. | Elegir arquitectura por calidad, tiempo y coste medidos en las mismas condiciones. |

### Pruebas que deciden si el diseño sirve

| Ensayo | Comportamiento esperado |
| --- | --- |
| Cambio real en producción y commit posterior distinto en la rama principal | Cita el SHA desplegado y la línea correcta. Reconoce el mecanismo del fallo y la dependencia expuesta. |
| Cambio presente solo en una PR o en staging | Puede señalar riesgo previsto. Descarta ese cambio como causa desplegada en producción. |
| Diagrama antiguo, logs incompletos o errores anteriores al release | Explicita la contradicción, intenta una comprobación independiente y se abstiene de recomendar un rollback sin respaldo. |
| Aprobación, rechazo y reenvío de la decisión | Cero rollbacks mientras espera o si se rechaza. Un rollback tras aprobar. Un solo efecto por decisión y verificación posterior antes de cerrar. |
| Reproducción antes del umbral de impacto | La investigación solo ve datos disponibles hasta la hora de corte. La recuperación y el postmortem del caso en curso solo se usan para evaluación posterior. |

Para comparar A, B y C, fijar el modelo, las herramientas, el presupuesto por caso y el conjunto de incidentes. Medir causas correctamente vinculadas al release, proporción de citas verificables, recomendaciones de rollback injustificadas, demoras, llamadas y coste por caso. **El margen de anticipación** se mide desde la recomendación hasta el umbral observado cuando existe un cruce histórico. Si la mitigación evita el cruce, reportar por separado el margen proyectado y el tiempo que tardó la investigación. Los 24 minutos del pitch son una proyección del caso sintético.

