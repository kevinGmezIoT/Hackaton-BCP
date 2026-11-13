# Vigía · MVP para la hackathon (Desafío 2)

> Anticipa la falla. Tú decides la acción.

Vigía es un agente de IA que detecta una degradación antes de que afecte a los usuarios, investiga la causa con herramientas, recomienda una acción preventiva y **espera la aprobación de una persona** antes de ejecutarla.

Este repositorio contiene el MVP que construimos en **4 horas**. Lee este documento completo antes de empezar: aquí está todo lo que necesitamos para trabajar en paralelo sin bloquearnos.

---

## 1. Qué vamos a demostrar

Un solo caso, de punta a punta:

1. La latencia de **Transferencias** empieza a subir (simulamos la alerta con un botón).
2. El agente **decide por sí mismo** qué herramientas usar: confirma la anomalía, encuentra el cambio `CHG-4821`, revisa los logs, encuentra incidentes parecidos y el runbook.
3. Propone **revertir `CHG-4821`** con su evidencia. Como el riesgo es alto, **se detiene**.
4. Una persona aprueba (o rechaza) desde la pantalla.
5. Se ejecuta la acción (simulada) y la latencia vuelve a la normalidad.

### Mínimo innegociable

> Alerta → al menos cuatro llamadas a herramientas decididas por el modelo → recomendación con evidencia → aprobación → la métrica se recupera.

Si esto funciona, hay demo. Todo lo demás es opcional.

---

## 2. Alcance

| Estado | Qué incluye |
|---|---|
| **Real** | El modelo elige herramientas y encadena pasos · herramientas de lectura sobre archivos de ejemplo · pausa y reanudación por aprobación · UI conectada al backend |
| **Simplificado** | Un solo agente (cada paso se etiqueta con un rol) · detección con z-score y proyección lineal · similitud con TF-IDF · tabla de riesgo en código · auditoría en `audit.jsonl` |
| **Simulado** | Fuentes de datos del banco (archivos locales) · llegada de la alerta (botón) · ejecución del rollback y del ticket |
| **Fuera** | Conectores reales, ingesta en tiempo real, motor de políticas, gestión de secretos, SSO, memoria de casos, varios agentes |

### Reglas para no prometer de más

- No inventamos cifras de negocio (porcentajes de reducción, ahorros, precisión). Solo usamos números que salen del escenario simulado y lo decimos.
- Lo simulado se declara como simulado, en la UI y en el pitch.
- El agente **solo propone** acciones. La ejecución la hace el backend después de la aprobación.
- Nada de datos reales del banco ni de clientes. Solo datos sintéticos.

---

## 3. Arquitectura

```
Usuario ──► UI web (una página) ──HTTP──► API (FastAPI)
                    ▲                         │
                    └──── eventos SSE ────────┤
                                              ▼
                                   Agente (bucle de tool calling) ◄──► Modelo LLM (API)
                                              │
                         ┌────────────────────┼─────────────────────┐
                         ▼                    ▼                     ▼
                  7 herramientas       Tabla de riesgo       audit.jsonl
                         │                    │
                         ▼                    ▼
                 Datos de ejemplo     Pausa por aprobación ──► Ejecutor simulado
```

Diagrama editable: `docs/vigia_mvp_arquitectura.drawio` (ábrelo en [app.diagrams.net](https://app.diagrams.net)).

### Estructura sugerida del repositorio

```
vigia/
├── README.md
├── requirements.txt
├── .env.example            # MODEL_API_KEY, MODEL_NAME
├── app.py                  # FastAPI: endpoints y SSE
├── agente.py               # bucle de tool calling, prompt, pausa
├── herramientas.py         # 7 herramientas + esquemas + auditoría
├── analitica.py            # z-score, proyección, TF-IDF
├── ejecutor.py             # acciones simuladas
├── data/
│   ├── metricas.csv
│   ├── logs.txt
│   ├── cambios.json
│   ├── incidentes.json
│   ├── servicios.yaml
│   └── runbooks/*.md
├── ui/
│   └── index.html          # adaptada de la maqueta
└── docs/
    └── vigia_mvp_arquitectura.drawio
```

### Cómo correrlo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # completar la clave del modelo
uvicorn app:app --reload --port 8000
# abrir http://localhost:8000
```

---

## 4. Contrato de interfaces

**Se acuerda en los primeros 20 minutos y no se cambia sin avisar a todos.** Con esto, cada frente avanza con respuestas de prueba.

### Herramientas

| Herramienta | Rol en la UI | Firma | Respuesta |
|---|---|---|---|
| `consultar_metricas` | Detección | `(servicio, metrica, minutos=30)` | `{puntos: [[hora, valor]], actual, hace_15_min, unidad}` |
| `detectar_anomalia` | Detección | `(servicio, metrica)` | `{anomalia, z, umbral, minutos_para_cruce}` |
| `listar_cambios` | Cambios | `(servicio, horas=3)` | `[{id, servicio, hora, descripcion}]` |
| `buscar_logs` | Diagnóstico | `(servicio, patron, minutos=15)` | `{total, primera_ocurrencia, ejemplos}` |
| `buscar_incidentes_similares` | Diagnóstico | `(descripcion, k=3)` | `[{id, similitud, causa_raiz, solucion}]` |
| `buscar_runbook` | Diagnóstico | `(consulta)` | `{id, titulo, pasos}` |
| `proponer_accion` | Recomendación | `(accion, objetivo, justificacion, evidencia)` | `{estado: ejecutada \| rechazada, detalle}` |

Tabla de riesgo (`proponer_accion` consulta esto):

```python
RIESGO = {
    "crear_ticket_preventivo": "bajo",   # automática
    "notificar_equipo": "bajo",          # automática
    "escalar_capacidad": "medio",        # requiere aprobación
    "revertir_cambio": "alto",           # requiere aprobación
}
```

### API entre la UI y el backend

```
POST /api/alerta      {servicio}          -> {caso}
POST /api/chat        {mensaje}           -> {respuesta, herramientas}
POST /api/aprobacion  {caso, decision}    -> {ok}            # decision: aprobar | rechazar
GET  /api/eventos     (SSE)               -> un evento por paso
GET  /api/metricas    ?servicio=          -> {puntos, umbral}
GET  /api/auditoria                       -> [registros]
```

### Evento que recibe la UI

```json
{
  "tipo": "paso",
  "caso": "CASO-1042",
  "hora": "09:12:15",
  "rol": "Cambios",
  "herramienta": "listar_cambios",
  "args": {"servicio": "transferencias", "horas": 3},
  "resumen": "CHG-4821 desplegado a las 08:55"
}
```

`tipo` puede ser `paso`, `propuesta`, `decision` o `mensaje`.

**Plan B** si SSE da problemas: la UI consulta `GET /api/caso` cada segundo. El formato del evento no cambia.

---

## 5. Datos del escenario

Todos los números de la demo deben salir de estos archivos, no estar escritos a mano.

| Archivo | Dato clave |
|---|---|
| `metricas.csv` | Latencia p95 estable en ~180 ms; sube 16 ms por minuto entre 08:57 y 09:12 hasta 420 ms; umbral de impacto 800 ms |
| `cambios.json` | `CHG-4821` a las 08:55 en `pagos-core`: pool de conexiones de 80 a 50 (más dos cambios sin relación) |
| `logs.txt` | 312 errores `Connection pool exhausted` desde las 09:01 |
| `incidentes.json` | 15 a 20 incidentes; `INC-3512` e `INC-3307` son del mismo patrón y se resolvieron con rollback; el resto no tiene relación |
| `runbooks/` | `RB-087`: ante pool agotado, rollback del cambio como primera acción |
| `servicios.yaml` | Transferencias depende de `pagos-core`; umbral y equipo dueño |

Resultado esperado: la proyección lineal da el cruce del umbral en **unos 24 minutos**.

---

## 6. Equipo y responsabilidades

| Integrante | Frente propio | Apoya en | Listo cuando… |
|---|---|---|---|
| **AI Engineer 1** | Agente | Integración con la UI | El modelo encadena ≥ 4 herramientas, propone la acción y se detiene hasta la aprobación |
| **AI Engineer 2** | Herramientas y auditoría | API del backend | Las 7 herramientas responden con datos del escenario y cada llamada queda en `audit.jsonl` |
| **Data Scientist** | Datos y analítica | Cifras del pitch | Los archivos son coherentes entre sí y la proyección da ~24 min |
| **QE** | Demo y calidad | UI | La demo sale igual tres veces seguidas y hay video de respaldo |
| **Tech Engineer / LT** | API, UI e integración | Pitch | Todo corre en una laptop con un comando y la pantalla muestra el caso completo |

### Tareas por integrante

**AI Engineer 1 · Agente**
- [ ] Probar clave y tool calling del modelo (primeros 20 min)
- [ ] Bucle del agente y prompt del sistema
- [ ] Pausa por aprobación y reanudación con la decisión (si se complica: versión de dos turnos)
- [ ] Emitir un evento por paso con el formato acordado
- [ ] Ajustar prompt y temperatura en los ensayos

**AI Engineer 2 · Herramientas y auditoría**
- [ ] Las 7 funciones con respuestas de prueba publicadas al minuto 20
- [ ] Conectarlas a los archivos de `data/`
- [ ] Ejecutor simulado: registra la acción y la métrica se recupera
- [ ] Registro de auditoría en todas las llamadas
- [ ] Apoyo en los endpoints de la API

**Data Scientist · Datos y analítica**
- [ ] Archivos del escenario en `data/`
- [ ] `detectar_anomalia`: z-score y proyección lineal
- [ ] `buscar_incidentes_similares`: TF-IDF + similitud coseno
- [ ] Verificar que los números de la demo salen de los datos
- [ ] Cifras del pitch, marcadas como escenario simulado

**QE · Demo y calidad**
- [ ] Guion de la demo (primeros 20 min)
- [ ] Revisar coherencia del escenario (horas, IDs, dependencias)
- [ ] Probar cada integración y el flujo completo, incluido rechazo, pregunta fuera del caso y modelo lento
- [ ] Apoyo en la UI: textos, estados vacíos, tabla de auditoría
- [ ] Video de respaldo a las 3:15 y cronómetro del ensayo

**Tech Engineer / LT · API, UI e integración**
- [ ] Repositorio, estructura y FastAPI con los 6 endpoints
- [ ] UI adaptada de la maqueta: gráfico, línea de tiempo, aprobación, consola
- [ ] Dirigir la integración y decidir recortes a las 2:45
- [ ] Congelamiento a las 3:15 y versión estable
- [ ] Ajustar y presentar el pitch

### Responsabilidades compartidas

- **Contrato (0:00–0:20):** lo acuerdan los cinco; AI Engineer 2 lo deja escrito en el repo.
- **Integración (1:45–2:45):** AI Eng 1 + Tech conectan agente y UI; AI Eng 2 + DS conectan herramientas y datos; QE prueba cada pieza.
- **Ensayo (3:15–4:00):** Tech presenta, QE opera la demo, el resto anota fallas sin tocar el código.

---

## 7. Cronograma

| Tiempo | Qué pasa | Resultado esperado |
|---|---|---|
| 0:00 – 0:20 | Todos: contrato, guion, repo y clave del modelo probada | Firmas y formato de evento acordados |
| 0:20 – 1:45 | Trabajo en paralelo por frente, con respuestas de prueba | Agente llamando herramientas falsas; datos listos; UI pintando eventos de prueba |
| 1:45 – 2:45 | Integración | Caso completo de punta a punta |
| 2:45 – 3:15 | Pruebas del guion, camino de rechazo, ajustes de prompt | Demo estable tres veces seguidas |
| **3:15** | **Congelamiento: no entra funcionalidad nueva** | Video de respaldo grabado |
| 3:15 – 4:00 | Ensayo cronometrado y pitch | Presentación lista |

### Orden de recorte si vamos atrasados

1. La consola pasa de consulta libre a tres preguntas sugeridas.
2. Se retira `buscar_runbook`.
3. SSE se reemplaza por consulta periódica.
4. La auditoría se muestra desde el archivo, sin tabla en la UI.

Decide el Tech Engineer / LT, a las 2:45.

---

## 8. Guion de la demo (≈ 2 minutos)

1. Mostrar la pantalla: Transferencias en estado normal.
2. Pulsar **Simular alerta**. Narrar mientras aparecen los pasos: «el agente decide qué revisar».
3. Abrir una llamada a herramienta para mostrar que es real (parámetros y resultado).
4. Leer la recomendación y su evidencia. Señalar que se detuvo por riesgo alto.
5. Pulsar **Aprobar**. Mostrar la recuperación en el gráfico y la tabla de auditoría.
6. (Opcional) En la consola: «¿Qué cambios se desplegaron hoy?».

---

## 9. Cómo responde el MVP a la rúbrica

| Criterio | Peso | Evidencia en la demo |
|---|---|---|
| Uso de IA agéntica | 25% | El modelo elige herramientas, se pausa por riesgo y continúa tras la decisión humana |
| Prototipo y experiencia de usuario | 25% | Una pantalla: estado, qué hizo el agente, qué debe decidir la persona |
| Impacto en Ingeniería o Negocio | 20% | ~24 min de anticipación y diagnóstico en < 1 min (escenario simulado) |
| Innovación y diferenciación | 15% | Cruza métricas, cambios e historial para proponer una acción concreta con control graduado |
| Factibilidad y escalabilidad | 15% | Corre en una laptop; cada herramienta tiene equivalente en una API real; un servicio nuevo = un archivo de configuración |

Bonos que apuntamos: productividad en SDLC (3), reducción de riesgos en producción (2), reutilización en varias tribus (2), implementación ≤ 6 meses (1), NPS de servicios CIO (1, como hipótesis).

---

## 10. Riesgos del día

| Riesgo | Mitigación |
|---|---|
| Falla la API del modelo o la red | Video de respaldo y capturas en el pitch |
| El modelo cambia el orden de los pasos | Temperatura baja, prompt con pasos esperados, datos fijos |
| La integración se atrasa | Contrato desde el minuto 20 y respuestas de prueba |
| La pausa por aprobación se complica | Versión de dos turnos: la aprobación inicia un nuevo turno con la decisión |
| Ganas de agregar algo a último momento | Congelamiento a las 3:15 |

---

## 11. Material de referencia

- **Propuesta técnica final** (alcance, arquitectura, contrato, plan, rúbrica)
- **Presentación del pitch** (11 diapositivas)
- **Maqueta de la UI** (la pantalla que vamos a construir)
- **Diagrama editable**: `docs/vigia_mvp_arquitectura.drawio`

Los tres primeros son páginas publicadas; pide el enlace al Tech Engineer / LT.

Si usas Claude, instala el skill **vigia-hackathon**: le da el mismo contexto, alcance y contrato que este README.
