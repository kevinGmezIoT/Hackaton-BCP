# Vigía — Detección temprana de fallas en servicios bancarios

Vigía es un agente de inteligencia artificial que ayuda al equipo responsable de un servicio bancario a intervenir antes de que una degradación afecte al cliente. Cuando el monitoreo detecta una señal temprana, el agente investiga qué cambió, estima cuánto tiempo queda para actuar y recomienda una medida concreta. El responsable del servicio recibe la recomendación junto con la evidencia y decide si autoriza la intervención cuando el riesgo es medio o alto.

## El problema que resuelve

Un servicio como Transferencias puede seguir disponible mientras empieza a responder cada vez más lento. Si la alerta llega recién cuando el tiempo de respuesta supera el límite de impacto, el cliente ya percibe la demora y el equipo de guardia apenas empieza a investigar.

El diagnóstico exige reunir información dispersa. El gráfico de latencia muestra **cuándo** empezó el deterioro, el registro de despliegues indica **qué** se modificó y los registros de errores ayudan a explicar **cómo** ese cambio pudo afectar al servicio. Incluso si otro equipo ya resolvió una falla parecida, la solución puede quedar en un informe posterior al incidente que nadie consulta durante la guardia. Reconstruir esa historia consume tiempo de ingeniería, duplica trabajo y prolonga la exposición del cliente a la falla.

## Cómo funciona la solución

1. **Confirma la señal y estima el margen de acción.** Ante una alerta del monitoreo, Vigía consulta la métrica, revisa cómo ha evolucionado y proyecta cuándo alcanzaría el umbral de impacto si la tendencia continúa.
2. **Investiga la causa probable.** Cruza el inicio de la desviación con cambios recientes y busca errores que expliquen el comportamiento observado. El modelo elige qué herramienta consultar a continuación según lo que encuentra.
3. **Recupera la experiencia del equipo.** Busca incidentes con un patrón similar, revisa qué intervención funcionó entonces y consulta las guías operativas disponibles.
4. **Presenta una decisión.** Propone una acción, indica su riesgo y cita las fuentes que la respaldan. Para medidas de riesgo medio o alto, el sistema espera la aprobación de una persona antes de ejecutar. Cada consulta, recomendación y decisión queda registrada con su evidencia.
5. **Verifica la recuperación.** Después de la intervención, Vigía vuelve a consultar la métrica para comprobar si el servicio recuperó su comportamiento habitual.

## Ejemplo del pitch — Latencia creciente en Transferencias

La demostración usa datos sintéticos. La métrica observada es la **latencia p95**, el tiempo dentro del cual finaliza el 95 % de las solicitudes. Así se pueden detectar demoras que afectan a las operaciones más lentas aunque el promedio todavía parezca aceptable.

| Hora | Qué ocurre en el escenario |
| --- | --- |
| 09:12:04 | El p95 subió de 180 a 420 ms en 15 minutos. Si continúa la tendencia, Vigía estima que alcanzará el umbral de 800 ms en unos 24 minutos. |
| 09:12:15 | El agente encuentra que el cambio `CHG-4821`, desplegado a las 08:55, redujo el número de conexiones disponibles en `pagos-core`. |
| 09:12:21 | Los registros muestran 312 errores por agotamiento de conexiones. En dos incidentes anteriores con el mismo patrón, el servicio se recuperó al revertir el cambio. |
| 09:12:41 | Vigía recomienda revertir `CHG-4821`. Clasifica la acción como de riesgo alto y solicita aprobación. Han pasado 37 segundos desde la alerta. |
| 09:14:05 | El líder del servicio aprueba la reversión. El sistema simula la ejecución y registra la decisión. |
| 09:19:02 | Vigía comprueba que el p95 volvió a 184 ms. En la simulación, el servicio se recuperó antes de alcanzar los 800 ms. |

## Valor para el negocio

| A quién beneficia | Qué valor aporta |
| --- | --- |
| Clientes del banco | Detectar el deterioro mientras una transferencia aún responde dentro del umbral da al equipo tiempo para intervenir. El beneficio esperado es reducir los incidentes que llegan a percibir los clientes. |
| Ingeniería y operaciones | El equipo recibe una hipótesis respaldada por el cambio, los errores y los antecedentes pertinentes. Dedica menos tiempo a reconstruir el incidente y puede concentrarse en decidir si la intervención es adecuada. |
| Responsables del servicio y de riesgo operativo | Pueden revisar la evidencia antes de autorizar una medida que afecte al servicio. El registro permite reconstruir después quién decidió, por qué y cómo respondió el servicio. |
| Equipos internos de TI | La consola permite hacer preguntas como «¿Qué cambios se desplegaron hoy?» y obtener la respuesta con sus fuentes. Cada tribu puede reutilizar el agente con un archivo de configuración por servicio. |

Los **24 minutos** representan el margen proyectado en el ejemplo y los **37 segundos** son el tiempo transcurrido en esa demo hasta la recomendación. Para cuantificar el beneficio en el banco, el piloto comparará el tiempo de diagnóstico y los incidentes con impacto al cliente con la línea base del servicio. También medirá los aciertos y las falsas alarmas para conocer la fiabilidad de las alertas.

## Qué cubre el prototipo

El producto mínimo viable construido para la hackatón incluye una interfaz web con el gráfico del servicio, la secuencia de investigación, la tarjeta de aprobación, la consola y el historial de auditoría. El agente dispone de siete herramientas para consultar métricas, detectar anomalías, listar cambios, buscar errores en los registros, recuperar incidentes similares, consultar guías operativas y proponer una acción. La interfaz permite ver qué herramienta utilizó y con qué parámetros.

En la demo, las herramientas leen archivos de ejemplo y la ejecución de acciones es simulada. Para producción, el diseño prevé conectar las herramientas a las API que el banco ya utiliza. La investigación requiere acceso de lectura a telemetría y metadatos operativos, sin datos de clientes.

## Ruta propuesta al piloto y a producción

| Plazo propuesto | Alcance |
| --- | --- |
| Meses 1 y 2 | Operar en **modo sombra** sobre un servicio. Vigía lee señales reales y se evalúa qué alertas anticipan incidentes y cuáles son falsas alarmas, sin intervenir en la operación. |
| Meses 3 y 4 | Enviar las recomendaciones con su evidencia al equipo responsable e integrarlas en su trabajo mediante tickets y avisos automáticos. |
| Meses 5 y 6 | Ampliar la cobertura a entre tres y cinco servicios de distintas tribus e incorporar las primeras acciones con aprobación humana. |

El siguiente paso es seleccionar el servicio del piloto y establecer su línea base. Para iniciarlo se requieren accesos de lectura a las fuentes operativas, un modelo de IA aprobado por el banco y la validación de seguridad.

Basado en el [pitch original de Vigía](<Pitch Vigía.html>).
