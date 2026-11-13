# Vigía · Diseño de investigación conversacional

**Objetivo de la demo.** El operador abre el caso de Transferencias y pregunta «¿Y si el problema fue el tráfico? Compara la tasa de errores antes y después del despliegue». Vigía entiende a qué recomendación se refiere, contrasta la hipótesis con datos y muestra por qué mantiene o retira la propuesta de revertir `CHG-4821`. Si la evidencia es insuficiente, identifica la medición pendiente. El operador puede inspeccionar tanto el diagnóstico original como su revisión.

**Decisión de arquitectura.** Mantener un único investigador y añadir una entrada conversacional ligada al caso. La capa de conversación interpreta la pregunta y recupera el contexto. El investigador existente ya sabe consultar despliegues, Git, métricas y logs. El backend conserva la autoridad sobre las propuestas y las acciones. Esta separación permite conversar en lenguaje natural y comprobar por separado si la comparación fue válida y si la acción seguía autorizada.

## 1. Qué existe y qué cambia

| Componente actual | Cambio propuesto |
| --- | --- |
| `CaseService.answer_query` busca palabras en evidencias y, cuando faltan coincidencias, devuelve hasta cinco evidencias del caso. | Mantener el método para sus consumidores actuales. Añadir un servicio de conversación que determine si el expediente responde la pregunta e indique qué información falta. |
| `InvestigatorAgent.run` recibe alerta, configuración y corte de la revisión. Al terminar actualiza la propuesta o el estado global del caso. | Reutilizar modelo, herramientas y presupuesto de llamadas en una investigación de seguimiento. Esta invocación recibe una pregunta y devuelve un hallazgo candidato. El backend publica los cambios una vez validados. |
| Las consultas de logs aceptan ventanas y cohortes. El registro de evidencia y las validaciones de reversión ya existen. | Incorporar una comparación reproducible de tasas y cobertura. Reutilizar evidencia válida para el corte solicitado. |
| SQLite almacena el expediente, sus revisiones, eventos y trabajo pendiente. La consola HTTP muestra el caso y sus decisiones. | Persistir turnos de conversación, vincularlos a revisiones y añadir el chat a la consola servida por el backend. |
| El backend comprueba permisos, modo y versión de propuesta. La aprobación actual puede despachar al ejecutor antes de guardar el caso con control de versión. | Hacer atómicas la invalidación por reevaluación y la reserva de una operación aprobada. Una tarjeta obsoleta debe producir cero despachos. |

[Servicio de casos](../src/vigia/application/case_service.py) · [Investigador](../src/vigia/application/investigator.py) · [Herramientas](../src/vigia/application/tools.py) · [Aprobaciones](../src/vigia/application/approval_service.py) · [Almacén SQLite](../src/vigia/adapters/outbound/case_store.py)

## 2. Dos tipos de turno

| Pregunta | Comportamiento | Efecto sobre el caso |
| --- | --- | --- |
| «¿Por qué recomiendas el rollback?» | Recuperar la propuesta a la que apunta la tarjeta, el cambio de código y las observaciones citadas. Responder con referencias que el usuario pueda abrir. | Se guarda el intercambio. La revisión y la propuesta siguen vigentes. |
| «¿También aumentaron los errores en las instancias que conservaron la versión anterior?» | Buscar primero si el expediente contiene tasas comparables de ambas cohortes. Si faltan, consultar las fuentes y evaluar qué explicación sostienen. | Una comprobación nueva que pueda afectar la recomendación genera una revisión. |
| «¿Y ahora?» | Si el turno anterior deja claro el servicio y la métrica, interpretar «ahora» como un nuevo corte explícito. Si sigue siendo ambiguo, preguntar qué quiere comprobar. | Conservar por separado los hallazgos de cada corte. |
| «Aprueba la reversión» | Mostrar la tarjeta vigente y su control de aprobación a quien tenga permiso. | El texto del chat no constituye una autorización. |

Para cada turno, el backend recupera el mensaje anterior relevante, la revisión visible, el servicio, el corte, la propuesta y sus evidencias. El modelo devuelve una intención estructurada, las referencias que considera suficientes o una pregunta comprobable. El backend comprueba que las referencias existen, corresponden al caso y cubren la comparación solicitada. Un texto del usuario se almacena como hipótesis hasta que alguna fuente lo respalde. Las guías e incidentes recuperados mediante RAG pueden orientar la investigación. Una causa probable de un incidente histórico sigue siendo un antecedente.

## 3. Cómo comprobar la hipótesis de tráfico

1. Obtener del registro de despliegue la hora, el entorno y las versiones que recibían tráfico. Fijar dos ventanas de igual duración alrededor del despliegue. Para responder «¿por qué lo recomendaste entonces?», ambas terminan como máximo en el corte de la recomendación. Para una consulta de situación actual, guardar el nuevo corte.
2. Consultar solicitudes y errores del mismo servicio, intervalo y cohorte. Usar como numerador solicitudes fallidas o un error definido y contado una vez por solicitud. Si la fuente solo tiene líneas de log que pueden duplicarse, identificar el indicador como eventos por solicitud y documentar esa limitación.
3. Calcular en código la tasa `errores / solicitudes` por ventana y cohorte. Guardar numerador, denominador, cobertura, definición del error y consulta reproducible. El total agregado de solicitudes del servicio no sirve como denominador de una sola versión. Si falta la etiqueta de versión o una cohorte tuvo cero tráfico, dejar la comparación correspondiente como indeterminada.
4. Comparar la evolución de la versión nueva con la cohorte estable cuando ambas estuvieron expuestas. El backend aplica mínimos de volumen y tolerancias configurados para el servicio. El modelo explica la interpretación y las limitaciones con citas. Empeorar a la vez en ambas versiones debilita la atribución al despliegue. Para atribuirlo al tráfico se requiere además evidencia de carga o saturación compartida.

| Resultado | Cambio visible |
| --- | --- |
| La evidencia diferencial sigue respaldando el cambio | **Recomendación mantenida.** Mostrar tasas, nuevas fuentes y versión vigente de la propuesta. |
| Una comparación válida contradice el fundamento del rollback | **Reversión retirada.** Explicar qué observación cambió el diagnóstico y qué revisar a continuación. |
| Falta cobertura para decidir | **No concluyente.** Identificar el dato faltante. Si afecta una condición necesaria para revertir, dejar la acción en revisión. Si la pregunta era secundaria, conservar la recomendación previa y declarar el alcance de la incertidumbre. |

## 4. Contrato del chat y memoria

**Entrada propuesta:** `POST /api/casos/{case_id}/mensajes`. La consola envía un identificador generado por el cliente, el texto, la revisión visible y, cuando procede, la propuesta consultada. El servidor obtiene la identidad de la sesión, aplica los permisos del caso y limita las consultas de herramientas a servicios autorizados.

~~~json
{
  "client_message_id": "msg-17",
  "text": "¿Y si fue el tráfico? Compara los errores por solicitud.",
  "base_revision": 1,
  "proposal_id": "PROP-1"
}
~~~

- Guardar el turno y su trabajo pendiente en la misma transacción. Responder `202` con `turn_id`, `case_id` y estado. Una clave repetida por el mismo actor y caso con el mismo contenido devuelve el turno existente. Un contenido distinto con esa clave devuelve `409`.
- Usar `GET /api/casos/{case_id}/mensajes` para recuperar el historial al recargar la página. Reutilizar `/api/eventos` para publicar avance y finalización con `turn_id` y secuencia. El cliente deduplica por secuencia.
- Persistir por turno pregunta, autor, revisión base, corte, intención, hipótesis, IDs de evidencia, respuesta y resultado. Guardar el resumen verificable del trabajo. Un turno puede fallar o reanudarse sin sobrescribir el diagnóstico publicado.
- Ante una tarjeta ya reemplazada, una explicación histórica puede referirse expresamente a esa revisión. Una reevaluación solicitada contra una revisión antigua devuelve `409` y la referencia vigente. Un fallo de conector queda visible como dato pendiente. La respuesta aclara qué pudo comprobarse.

## 5. Reevaluación y aprobación concurrentes

El caso tiene tres identidades distintas: la versión de almacenamiento para concurrencia, la revisión del diagnóstico para trazabilidad y la versión de propuesta que vio el aprobador. Conservar las tres. Añadir historial inmutable de propuestas y resultados por revisión, además del puntero a la propuesta vigente. Para expedientes anteriores, inicializar el historial con la propuesta existente al leerlos.

1. **Iniciar la reevaluación.** Cuando la pregunta cuestiona un fundamento necesario de una acción pendiente, marcar esa propuesta «en revisión» y encolar la investigación en una transacción condicionada a la versión vigente. Una explicación ordinaria no bloquea la aprobación.
2. **Investigar en lectura.** Registrar observaciones del turno con sus citas y corte. El trabajador de seguimiento solo dispone de herramientas de lectura. Devuelve el candidato al backend. Esto evita que una llamada a `propose_action` dispare efectos de bajo riesgo durante la conversación.
3. **Publicar.** En una transacción, comprobar que la revisión base sigue vigente, guardar el resultado, las citas y el evento de auditoría. Si la acción se mantiene, publicar su nueva versión. Si se retira, conservar el original con estado histórico y quitar la acción aprobable. Un trabajador retrasado que encuentre una revisión más reciente conserva su resultado como histórico y no la reemplaza.
4. **Aprobar.** Requerir ID y versión de la tarjeta para la aprobación web. Validar permisos, modo asistido, estado pendiente y vigencia. Reservar decisión y orden de ejecución en la misma transacción. Despachar después de confirmar la reserva, con clave de operación estable y revalidación de la versión desplegada. En sombra, una revisión humana se guarda como evaluación. Un turno conversacional nunca despacha al pipeline.
5. **Resolver carreras y fallos.** Si la reevaluación gana la reserva, la aprobación antigua devuelve `409` sin invocar al ejecutor. Si la aprobación ganó primero, mostrar la operación ya autorizada y tratar los hallazgos posteriores como seguimiento. Si el proceso cae tras enviar la orden, reconciliar por la clave externa antes de repetir. Si falla una investigación material, publicar el motivo y permitir reintentarla. La propuesta cuestionada conserva su estado «en revisión» hasta que exista una resolución explícita.

## 6. Cambios en el repositorio

| Dónde | Implementación |
| --- | --- |
| [Contratos](../src/vigia/domain/contracts.py) y [puertos](../src/vigia/domain/ports.py) | Añadir turno, resultado de comparación e historial de propuestas. Ampliar los contratos de persistencia para escritura atómica de turno, publicación de una revisión y reserva de operación. Usar valores por defecto compatibles con casos previos. |
| [Servicio de casos](../src/vigia/application/case_service.py) y [almacén](../src/vigia/adapters/outbound/case_store.py) | Incorporar el servicio de conversación y el tipo de trabajo de seguimiento. Añadir migración SQLite, unicidad por mensaje y por operación, recuperación de trabajos interrumpidos y publicación condicionada a la versión. |
| [Investigador](../src/vigia/application/investigator.py), [prompts](../src/vigia/application/prompts.py) y [herramientas](../src/vigia/application/tools.py) | Construir el contexto de un turno, separar la respuesta sobre evidencia existente de la comprobación nueva y devolver un resultado estructurado. Reutilizar consultas con ventana y cohorte. Añadir el cálculo de comparación reutilizable y verificar citas antes de publicar. |
| [Aprobaciones](../src/vigia/application/approval_service.py) | Reconocer propuestas en revisión o retiradas, validar la versión de la tarjeta y separar reserva transaccional de despacho externo. |
| [API y consola operativa](../src/vigia/adapters/inbound/http_api.py) | Añadir rutas e historial de mensajes, indicador de investigación, fuentes consultables y tarjeta que compare conclusión anterior y vigente. Mantener la revisión humana de sombra separada de la revisión diagnóstica. |

## 7. Demo y criterios de aceptación

Preparar dos variantes sintéticas con la misma alerta, el mismo despliegue, la misma recomendación inicial y la misma pregunta. La primera investigación debe tener evidencia suficiente para su recomendación inicial y dejar documentada la dimensión aún sin comprobar. El turno de seguimiento añade la comparación de tasas. Las cifras siguientes son datos propuestos para las pruebas.

| Variante | Datos de ventanas comparables | Resultado esperado |
| --- | --- | --- |
| Cambio causal plausible | Antes, 10 fallos en 10 000 solicitudes. Después, la cohorte estable registra 5 en 5 000 y la nueva registra 100 en 5 000. | Vigía muestra 0,1 % frente a 2 % después del cambio y mantiene la recomendación con evidencia diferencial. |
| Carga compartida | Misma base previa. Después se registran 200 fallos en 10 000 solicitudes en cada cohorte. La telemetría también muestra saturación del recurso compartido. | Ambas cohortes llegan al 2 % y el volumen total se duplica. Vigía retira la reversión como respuesta específica al despliegue y propone revisar el recurso saturado. |
| Cobertura insuficiente | Repetir la pregunta sin denominador fiable para una cohorte. | Respuesta indeterminada, fuente faltante visible y ausencia de porcentajes inventados. |

**Secuencia de entrega.** Primero, chat conectado al expediente con citas e historial. Después, seguimiento de solo lectura con las tres variantes y comparación visible de revisiones. Finalmente, activar el camino asistido cuando supere la prueba concurrente de aprobación. La demo de reevaluación se puede presentar en sombra mientras se implementa la reserva de ejecución.

**Prueba de punta a punta.** Abrir la tarjeta original en dos sesiones. Preguntar primero «¿por qué?» y comprobar que la respuesta cita evidencia sin nuevas lecturas operativas. Cuestionar luego la hipótesis de despliegue, observar qué consulta se lanza y abrir sus denominadores. Comprobar que el mismo modelo y la misma pregunta producen los resultados previstos al cambiar únicamente los datos. En modo asistido, intentar aprobar la tarjeta retirada desde la segunda sesión y verificar tanto el `409` como **cero llamadas al ejecutor**. Reenviar el mensaje y reiniciar el trabajador debe conservar un solo turno y una sola revisión publicada.
