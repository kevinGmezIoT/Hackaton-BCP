# Vigía y FrontendHackatonCIO en servidores separados

El frontend y su puente conservan el contrato de `FrontendHackatonCIO/api/README.md`. Las adaptaciones viven en Vigía. El navegador accede al servidor del frontend. Ese servidor recibe eventos del agente y hace las llamadas opcionales de chat e investigación hacia el servidor de Vigía.

## Configuración de las dos laptops

| Proceso | Archivo de configuración | Valores que deben acordarse |
| --- | --- | --- |
| Vigía | `.env` en la raíz del agente | `VIGIA_HOST=0.0.0.0`, `VIGIA_PORT=8001`, `VIGIA_BRIDGE_URL=http://IP_FRONTEND:8000` y `VIGIA_BRIDGE_TOKEN`. Las rutas de ingesta, decisión y salud usan las variables de la plantilla del agente. |
| Puente del frontend | `api/.env` en su servidor | `VIGIA_TOKEN` debe coincidir con `VIGIA_BRIDGE_TOKEN`. Para activar chat y revisión manual, `AGENTE_URL=http://IP_AGENTE:8001`. |
| Navegador | Compilación live del frontend | `npm run build:live`, seguido de `npm run puente` en el servidor del frontend. `VITE_API_URL` vacío permite usar el mismo origen que sirve la UI. |

El equipo configura las IP de red que sean accesibles entre ambas máquinas. `localhost` hace referencia a la propia máquina de cada proceso. El token permanece en los servidores. El puerto 8000 del puente y el 8001 de Vigía son ejemplos configurables. Para conservar el expediente y la posición de entrega, configurar `VIGIA_DB_PATH` con una ruta persistente.

El servicio canónico del piloto es `consulta_estado_cuenta`. El catálogo `config/services.yaml` lo registra con ese nombre y conserva los alias de la UI anterior para traducir sus peticiones. Configurar `VIGIA_CATALOG_PATH=./config/services.yaml`, `VIGIA_SERVICE=consulta_estado_cuenta` y `VIGIA_BRIDGE_SERVICE=consulta_estado_cuenta`. En `VIGIA_GITHUB_REPOS`, la clave del servicio también debe ser `consulta_estado_cuenta`.

El catálogo del piloto autoriza únicamente ese servicio. Su diagrama `config/consulta_estado_cuenta.drawio` no declara dependencias hasta que se confirmen las reales. Las rutas de diagramas se resuelven junto al catálogo y, para configuraciones anteriores, se conserva la búsqueda en `VIGIA_FIXTURES_DIR`. Configurar `VIGIA_INCIDENTS_PATH=./config/incidents.json` y `VIGIA_RUNBOOKS_DIR=./config/runbooks` para separar el conocimiento real del piloto de los incidentes y runbooks de ejemplo sobre `pagos-core`. Estas fuentes comienzan vacías y deben llenarse con antecedentes reales. Reiniciar Vigía y abrir un caso nuevo después de cambiar las fuentes; los expedientes anteriores conservan su historial.

Para la fuente REST de métricas, configurar `VIGIA_METRICS_API_URL=http://10.233.43.117:8009/api/v1/metricas/historico.txt` y dejar `VIGIA_METRICS_SERVICE_MAP` vacío. Aunque termina en `.txt`, la respuesta contiene JSON. El agente descarga el histórico sin parámetros y selecciona `consulta_estado_cuenta` dentro de `servicios`. Aplica localmente la ventana solicitada por la herramienta y su fecha de corte; si el servidor conserva menos historia, devuelve únicamente lo disponible y refleja la cobertura real. Las respuestas anteriores con pares `[fecha, valor]` también son compatibles.

Cada objeto de `puntos` aporta `desde_utc`, `hasta_utc`, `peticiones`, `errores` y `latencia_p95_ms`. El agente usa el p95 en milisegundos, fecha cada lectura al final del intervalo y suma los contadores de los intervalos completos incluidos. Convierte las fechas con su zona horaria y excluye intervalos parciales o posteriores al corte. Los huecos, los servicios sin puntos y las latencias nulas no generan ceros ni lecturas inventadas. Conserva los contadores conocidos incluso cuando no hay p95 medido. La consulta de otras métricas no disponibles devuelve ausencia de datos. La verificación de recuperación descarta los intervalos que comenzaron antes de ejecutar la acción y exige continuidad entre ventanas saludables.

En Vigía se mantienen las rutas de callback `/chat`, `/investigar` y `/decision`, que el puente ya usa. Sus equivalentes de configuración son `VIGIA_BRIDGE_CHAT_PATH`, `VIGIA_BRIDGE_INVESTIGATE_PATH` y `VIGIA_BRIDGE_NOTIFY_PATH`. Conservar esos valores cuando se use el frontend original. `VIGIA_BRIDGE_SERVICE` permite fijar el servicio al que se refieren las preguntas del chat. `VIGIA_BRIDGE_UTC_OFFSET=-05:00` presenta las marcas del agente en hora de Lima.

## Compatibilidad implementada en el agente

| Canal | Comportamiento |
| --- | --- |
| Pasos de herramientas | Se publican con ID estable, `hora`, resumen en español y un rol que la UI admite. Las consultas de arquitectura y código se agrupan en Diagnóstico. |
| Marca de despliegue | `listar_cambios.resultado` es una lista con ID y hora de cada despliegue. La UI original requiere una lista para crear el marcador. |
| Primer error | `buscar_logs.resultado.primera_ocurrencia` contiene la hora local del primer registro observado. |
| Acción | El nombre visible conserva `revertir_cambio`, `escalar_capacidad` u otra acción admitida. Se publica como ejecutada cuando el ejecutor confirma su resultado. |
| Cierre | El agente envía el mensaje de cierre después de verificar recuperación. La explicación inicial se conserva en la propuesta y sus evidencias. Una verificación todavía pendiente llega como paso. La UI original interpreta cualquier mensaje posterior a aprobar como cierre, por lo que esa distinción se hace al publicar desde Vigía. |
| Riesgo bajo y sombra | Se publican como pasos de recomendación. Se evita generar una tarjeta que el frontend convertiría en botones de aprobación. |
| Reconexión del puente | Se consultan su salud y la posición de entrega. Si pierde todos los eventos, se reenvía el expediente con los mismos IDs. Una decisión ya confirmada se reconstruye para que el historial conserve su secuencia. |

## Chat e investigación manual

El puente envía `POST /chat` con `{mensaje}`. Vigía selecciona el caso más reciente del servicio configurado, incorpora el expediente y devuelve `{respuesta, herramientas, fuentes}`. Una pregunta que requiere nuevas consultas se encola en el trabajador existente. La respuesta HTTP espera hasta `VIGIA_BRIDGE_CHAT_TIMEOUT` segundos, por defecto 45, dentro del límite de 60 segundos del puente. Si sigue pendiente, el agente devuelve 504 y el caso conserva el trabajo en curso.

Las preguntas libres, como «cómo van los logs del servicio» o «cuánto fue la última latencia registrada», se envían al modelo con servicio, métrica, diagnóstico, evidencias y conversación previa. Los mensajes cortos pueden apoyarse en ese contexto. El modelo puede responder con datos del expediente o consultar herramientas de lectura. Una pregunta informativa conserva la propuesta y la revisión diagnóstica vigentes, incluso si la aprobación está bloqueada por una reevaluación. La consola registra la intención y el estado del turno con el prefijo `[vigia-chat]`.

El callback `POST /investigar` recibe `{servicio}` y abre una investigación con una hora de corte nueva. Si la fuente carece de muestras o falla, responde igualmente 202 y registra esa limitación en la alerta. Vigía continúa con logs, cambios, código y procedimientos. La latencia actual y el margen quedan pendientes de medición. La respuesta `{caso}` identifica el expediente que luego aparece por sus eventos.

El callback `POST /decision` confirma la recepción del aviso directo del puente. El voto definitivo se consume por long-poll, con una sola ruta para invocar al servicio de aprobaciones. Los tres callbacks exigen `X-Vigia-Token` con el secreto acordado.

## Reevaluaciones con el contrato original

El puente correlaciona los votos exclusivamente por `caso`. Para impedir que una decisión de una tarjeta anterior autorice otra revisión, el adaptador publica cada revisión nueva bajo un identificador distinto. Por ejemplo, `CASE-1042` y `CASE-1042-R2` corresponden a revisiones del mismo expediente interno. El frontend selecciona el caso del evento más reciente.

Si la revisión mantiene la acción, publica una propuesta en `CASE-1042-R2` y consulta la decisión de ese identificador. Si retira el rollback o queda inconclusa, publica el hallazgo en esa revisión y conserva el bloqueo correspondiente en Vigía. Un clic en una pestaña antigua puede quedar registrado en el puente, pero no autoriza la revisión nueva. El agente revalida estado y versión antes de ejecutar.

La pantalla original calcula la banda de margen mediante una regresión sobre las métricas que recibe el puente. Vigía entrega además `minutos_para_cruce` como resultado de herramienta. Para que ambas cifras representen el mismo fenómeno, acordar métrica, umbral, ventanas y definición del p95. El gráfico del puente calcula p95 sobre solicitudes exitosas y cuenta los errores aparte.

## Verificación y alcance

Las pruebas del agente usan el puente real del directorio frontend, levantan ambos servidores HTTP en puertos distintos y comprueban token, aprobación, rechazo, chat, revisión de hipótesis, tarjetas anteriores y reenvío del historial. El código del frontend queda en su versión original.

La ejecución de rollback continúa simulada hasta que el equipo del servidor de la HU configure su endpoint o pipeline. La justificación y los pasos indican el efecto simulado. El envío de métricas crudas sigue a cargo del emisor acordado, por ejemplo el generador de usuarios. El agente evita duplicarlas automáticamente.
