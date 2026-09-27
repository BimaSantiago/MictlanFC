# Prompt2Contract

MVP de hackathon: convierte un prompt en lenguaje natural en un contrato Soroban (Stellar)
compilado, probado, auditado y desplegado en testnet.

## Flujo

1. El frontend (`index.html`, estático, sin build) manda el prompt al backend.
2. El backend (`main.py`, FastAPI) le pide el código a Gemini en un formato de 3 bloques:
   `### LIB` (lib.rs), `### TEST` (test.rs) y `### FUNCTIONS` (manifiesto JSON de funciones
   públicas, usado para generar botones de invocación).
3. Cada request genera su propio paquete Cargo aislado en `contracts/gen_<id>/` (nunca pisa
   `contracts/hello-world/`, que queda como plantilla de referencia).
4. **Loop de autorreparación**: corre `cargo test`; si falla, le reenvía el error del
   compilador a Gemini y reintenta, hasta 3 veces. Solo si los tests pasan se compila el WASM
   con `stellar contract build`.
5. Un segundo pase de Gemini audita el contrato (`SYSTEM_PROMPT_AUDIT` en `main.py`) y
   devuelve una lista de riesgos (p. ej. falta de `require_auth()`), informativa y no
   bloqueante.
6. Se despliega a testnet firmando con la identidad configurada (`alice` por defecto).
7. El frontend muestra el Contract ID, el explorador, los hallazgos de seguridad y una tarjeta
   por función pública para invocarla contra testnet (`POST /invoke`).

## Correr en local

Backend:

```sh
cd mi_contrato_base
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # y completa GEMINI_API_KEY
uvicorn main:app --reload
```

El backend queda en `http://127.0.0.1:8000`. Al arrancar, si no existe la identidad de Stellar
configurada (`alice` por defecto), la crea y la financia en testnet vía Friendbot
automáticamente.

Frontend: abre `index.html` directamente en el navegador. Por defecto apunta a
`http://127.0.0.1:8000`; para apuntar a un backend desplegado usa
`index.html?api=https://tu-backend.onrender.com`.

## Correr con Docker (recomendado si no quieres instalar Rust/Stellar CLI a mano)

El `Dockerfile` incluido instala Rust, el target `wasm32v1-none` y `stellar-cli` dentro del
contenedor, así que no necesitas nada de eso en tu máquina — solo Docker.

Con `docker compose` (backend + un nginx sirviendo `index.html` en `:8080`):

```sh
cd mi_contrato_base
copy .env.example .env          # y completa GEMINI_API_KEY
docker compose up --build
```

- Backend: `http://localhost:8000`
- Frontend: `http://localhost:8080`
- La identidad `alice` se genera/financia la primera vez y persiste en el volumen
  `stellar-identity` entre reinicios (`docker compose down` sin `-v` no la borra).

Solo el backend, sin compose:

```sh
docker build -t prompt2contract .
docker run -p 8000:8000 --env-file .env prompt2contract
```

La primera build tarda varios minutos (compila el toolchain de Rust y `stellar-cli` desde
cero); las siguientes reusan la caché de capas de Docker y son mucho más rápidas.

## Variables de entorno (`.env`, ver `.env.example`)

- `GEMINI_API_KEY` — key de Google AI Studio. Nunca se commitea.
- `ALLOWED_ORIGINS` — orígenes permitidos por CORS, separados por coma. `*` solo para
  demo/desarrollo; en producción real restringir al dominio del frontend desplegado.
- `STELLAR_SOURCE_IDENTITY` — identidad de `stellar` CLI usada para compilar/desplegar/invocar
  (`alice` por defecto).
- `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` — límite de solicitudes por IP a
  `/generate-and-deploy` y `/invoke` (protege los fondos testnet de la identidad de despliegue).

## Desplegar (Render / Railway / Apex)

El backend no es un servicio Python "normal": cada request compila Rust a WASM y llama al
Stellar CLI, así que necesita el toolchain de Rust (`wasm32v1-none`) y `stellar-cli`
instalados, no solo `pip install`. Por eso el despliegue se hace con el `Dockerfile` incluido
(instala Rust + `stellar-cli` sobre `python:3.12-slim`), no con el runtime nativo de Python de
la plataforma.

1. En Render: "New Web Service" → conectar el repo → runtime **Docker** (usa
   `render.yaml`/`Dockerfile` de este directorio).
2. Configura `GEMINI_API_KEY` como secret en el panel de la plataforma (no va en `render.yaml`).
3. La primera vez que arranca, el contenedor genera y financia la identidad `alice` vía
   Friendbot. **Ojo:** en un plan sin disco persistente, esa identidad se pierde en cada
   redeploy/reinicio y se vuelve a generar (una nueva testnet identity, sin fondos previos
   hasta el próximo `fund`). `render.yaml` monta un disco en `/root/.config/stellar` para
   mitigar esto; si tu plan no soporta discos, considera pre-generar y fondear una identidad
   fuera del proceso de arranque.
4. Actualiza el `API_BASE` del frontend (`index.html?api=...`) o el default en el `<script>`
   para que apunte a la URL pública del backend.

**Sobre Apex** (plataforma del hackathon): no tengo todavía sus requisitos exactos (comando de
build, carpeta de salida, formato de variables de entorno), así que el proyecto quedó
preparado de forma genérica con el mismo `Dockerfile`/`docker-compose.yml` que usa Render —
si Apex acepta un contenedor Docker (como Render/Railway), debería funcionar igual: build con
`Dockerfile`, variable de entorno `GEMINI_API_KEY` como secret, y `PORT` ya se respeta en el
`CMD` (`uvicorn ... --port ${PORT:-8000}`). Si Apex es un hosting estático o tiene su propio
formato de manifiesto, hace falta ajustar esto con su documentación real.

## Limitaciones conocidas

- **Sin aislamiento de contenedor/proceso real.** El código Rust generado por el LLM se
  compila en el mismo proceso/máquina que sirve el backend. Se mitigan los riesgos más obvios
  con: paquete Cargo aislado por request, rate limiting por IP, y timeouts estrictos en cada
  subprocess (`cargo test`, `stellar contract build/deploy/invoke`). No hay límites de
  CPU/memoria por proceso (no soportado de forma nativa por `subprocess` en Windows); para un
  entorno real de producción, correr la compilación dentro de un contenedor efímero con
  límites de recursos (Docker + `--memory`/`--cpus`, o un sandbox tipo gVisor/Firecracker).
- **Firma del despliegue centralizada.** El deploy y las invocaciones siempre los firma la
  identidad del servidor (`alice`), no la wallet Freighter del usuario. Al conectar Freighter
  se usa su dirección como owner/admin sugerido (se puede pasar como argumento al invocar
  `initialize`), pero la propiedad real de gas/autoría de la transacción sigue siendo del
  servidor. Firma real end-to-end (construir el XDR sin firmar, firmarlo en el navegador con
  Freighter y someterlo) queda como trabajo futuro.
- **Rate limiting en memoria de un solo proceso.** No sobrevive a un restart ni escala a
  múltiples instancias; para eso hace falta un store compartido (Redis) o el rate limiter de
  la plataforma de hosting.
