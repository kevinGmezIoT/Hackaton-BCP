# Vigía y FrontendHackatonCIO en servidores separados

El frontend y su puente conservan el contrato de `FrontendHackatonCIO/api/README.md`. Las adaptaciones viven en Vigía. El navegador accede al servidor del frontend. Ese servidor recibe eventos del agente y hace las llamadas opcionales de chat e investigación hacia el servidor de Vigía.

## Configuración de las dos laptops

| Proceso | Archivo de configuración | Valores que deben acordarse |
| --- | --- | --- |
| Vigía | `.env` en la raíz del agente | `VIGIA_HOST=0.0.0.0`, `VIGIA_PORT=8001`, `VIGIA_BRIDGE_URL=http://IP_FRONTEND:8000` y `VIGIA_BRIDGE_TOKEN`. Las rutas de ingesta, decisión y salud usan las variables de la plantilla del agente. |
| Puente del frontend | `api/.env` en su servidor | `VIGIA_TOKEN` debe coincidir con `VIGIA_BRIDGE_TOKEN`. Para activar chat y revisión manual, `AGENTE_URL=http://IP_AGENTE:8001`. |
| Navegador | Compilación live del frontend | `npm run build:live`, seguido de `npm run puente` en el servidor del frontend. `VITE_API_URL` vacío permite usar el mismo origen que sirve la UI. |

El equipo configura las IP de red que sean accesibles entre ambas máquinas. `localhost` hace referencia a la propia máquina de cada proceso. El token permanece en los servidores. El puerto 8000 del puente y el 8001 de Vigía son ejemplos configurables. Para conservar el expediente y la posición de entrega, configurar `VIGIA_DB_PATH` con una ruta persistente.

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
