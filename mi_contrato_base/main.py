import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = BASE_DIR / "contracts"
WASM_DIR = BASE_DIR / "target" / "wasm32v1-none" / "release"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY no está definida. Copia .env.example a .env y completa la key."
    )

_allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = (
    ["*"]
    if _allowed_origins_raw == "*"
    else [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]
)

STELLAR_SOURCE_IDENTITY = os.environ.get("STELLAR_SOURCE_IDENTITY", "alice")
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "5"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "600"))

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash-lite")

MAX_GENERATION_ATTEMPTS = 3
# soroban-sdk es una dependencia pesada: la primera compilación en un contenedor
# sin caché de cargo (target/ vacío) puede tardar varios minutos. Compilaciones
# posteriores en el mismo contenedor reusan la caché y son mucho más rápidas.
CARGO_TEST_TIMEOUT = 300
BUILD_TIMEOUT = 300
DEPLOY_TIMEOUT = 60
INVOKE_TIMEOUT = 60
AUDIT_TIMEOUT_NOTE = "La auditoría es best-effort y nunca bloquea el despliegue."

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=GEMINI_API_KEY)


# --- AUTOMATIZACIÓN DE LA CUENTA DE DESPLIEGUE PARA EL MVP ---
def preparar_cuenta_stellar():
    print(f"\n[SISTEMA] Verificando cuenta de despliegue '{STELLAR_SOURCE_IDENTITY}'...")
    try:
        resultado = subprocess.run(
            ["stellar", "keys", "ls"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
        if STELLAR_SOURCE_IDENTITY not in resultado.stdout:
            print(f"[SISTEMA] No existe '{STELLAR_SOURCE_IDENTITY}'. Generando nuevas llaves...")
            subprocess.run(
                ["stellar", "keys", "generate", STELLAR_SOURCE_IDENTITY, "--network", "testnet"],
                check=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            print("[SISTEMA] Pidiendo XLM de prueba a Friendbot...")
            subprocess.run(
                ["stellar", "keys", "fund", STELLAR_SOURCE_IDENTITY, "--network", "testnet"],
                check=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            print(f"[SISTEMA] Cuenta '{STELLAR_SOURCE_IDENTITY}' creada y financiada con éxito.")
        else:
            print(f"[SISTEMA] La cuenta '{STELLAR_SOURCE_IDENTITY}' ya está configurada y lista.")
    except Exception as e:
        print(f"[ERROR] No se pudo configurar la cuenta de Stellar: {e}")


preparar_cuenta_stellar()


# --- RATE LIMITING (en memoria, por IP; ver README para límites de este enfoque) ---
_rate_limit_lock = threading.Lock()
_rate_limit_hits: dict[str, list[float]] = {}


def check_rate_limit(ip: str):
    now = time.time()
    with _rate_limit_lock:
        hits = [t for t in _rate_limit_hits.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail="Demasiadas solicitudes desde esta IP. Espera unos minutos e intenta de nuevo.",
            )
        hits.append(now)
        _rate_limit_hits[ip] = hits


# --- MANIFIESTO DE FUNCIONES POR CONTRATO DESPLEGADO (para /invoke) ---
_contract_lock = threading.Lock()
_contract_functions: dict[str, list[dict]] = {}


class ContractRequest(BaseModel):
    prompt: str
    owner_address: Optional[str] = None


class InvokeRequest(BaseModel):
    contract_id: str
    function: str
    args: dict[str, str] = {}


SYSTEM_PROMPT = """
Eres un experto desarrollador de Smart Contracts en Soroban (Stellar) usando Rust.
Tu tarea es convertir la solicitud en lenguaje natural del usuario en un contrato completo
y funcional, junto con sus tests y un manifiesto de sus funciones públicas.

REGLAS ESTRICTAS DE SALIDA:
1. Responde ÚNICAMENTE con tres bloques, en este orden exacto, sin texto antes, entre o después:

### LIB
```rust
<código completo de lib.rs>
```
### TEST
```rust
<código completo de test.rs>
```
### FUNCTIONS
```json
<array JSON con una entrada por función pública del contrato>
```

2. lib.rs DEBE incluir `#![no_std]`, `mod test;` (para que los tests se compilen), y usar
   `soroban_sdk::{contract, contractimpl, contracttype, contracterror, Env, Address, ...}` según
   se necesite.
3. Cualquier función que reciba una `Address` que represente al dueño/admin/autor de una acción
   DEBE llamar `<esa_address>.require_auth()` como primera línea del cuerpo. Nunca generes una
   función de inicialización o administración sin esa verificación.
4. test.rs DEBE empezar EXACTAMENTE con la línea `#![cfg(test)]` (sin eso, `stellar contract
   build` falla: compila test.rs en el build de release de WASM, donde `testutils` no está
   disponible, aunque `cargo test` sí pase). Después de esa línea usa `use super::*;`,
   `soroban_sdk::testutils::Address as _`, `Address::generate(&env)`, y `env.mock_all_auths();`
   en cada test, y cubre al menos el camino feliz de cada función pública.
5. El bloque FUNCTIONS es un array JSON de objetos con esta forma exacta:
   {"name": "nombre_funcion", "args": [{"name": "arg", "type": "address|u32|i128|string|bool"}], "returns": "descripcion corta"}
   Debe listar TODAS las funciones públicas del contrato (impl con #[contractimpl]), en el mismo
   orden en que aparecen en lib.rs, y los tipos deben ser exactamente uno de: address, u32, i128,
   string, bool.
6. El código debe ser sintácticamente correcto para soroban-sdk 27.
"""

SYSTEM_PROMPT_AUDIT = """
Eres un auditor de seguridad experto en contratos Soroban (Stellar) escritos en Rust.
Se te da el código de un contrato. Responde ÚNICAMENTE con un array JSON (sin texto adicional,
sin bloque markdown) de hallazgos de seguridad, cada uno con esta forma:
{"severity": "high|medium|low", "description": "explicación breve y concreta", "location": "función o línea aproximada"}

Revisa en particular: funciones que cambian estado o mueven fondos sin require_auth() del
address correspondiente, posibles overflow/underflow en aritmética, falta de validación de
montos negativos o cero, condiciones de reentrancia, y falta de checks de inicialización.
Si no encuentras hallazgos, responde con un array vacío: []
"""


def _extraer_bloque(texto: str, etiqueta: str, lenguaje: str) -> str:
    patron = rf"###\s*{etiqueta}\s*```{lenguaje}\s*(.*?)```"
    match = re.search(patron, texto, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError(
            f"No encontré el bloque ### {etiqueta} con un ```{lenguaje} ... ``` bien formado."
        )
    return match.group(1).strip()


def parsear_respuesta_gemini(texto: str) -> tuple[str, str, list[dict]]:
    lib_code = _extraer_bloque(texto, "LIB", "rust")
    test_code = _extraer_bloque(texto, "TEST", "rust")
    functions_raw = _extraer_bloque(texto, "FUNCTIONS", "json")
    try:
        functions = json.loads(functions_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"El bloque ### FUNCTIONS no es JSON válido: {e}")
    if not isinstance(functions, list):
        raise ValueError("El bloque ### FUNCTIONS debe ser un array JSON.")
    return lib_code, test_code, functions


GEMINI_TRANSIENT_RETRIES = 4
GEMINI_TRANSIENT_MARKERS = (
    "503", "429", "500", "502", "504",
    "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "INTERNAL",
)


def _llamar_gemini(contents: str, intentos: int = GEMINI_TRANSIENT_RETRIES) -> str:
    """Llama a Gemini con reintentos + backoff para errores transitorios del
    servicio (503 sobrecargado, 429 rate limit, etc). Errores no transitorios
    (prompt bloqueado, key inválida, etc) se propagan de inmediato."""
    ultimo_error: Optional[Exception] = None
    for intento in range(1, intentos + 1):
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=contents)
            return response.text
        except Exception as e:
            ultimo_error = e
            es_transitorio = any(marca in str(e) for marca in GEMINI_TRANSIENT_MARKERS)
            if not es_transitorio or intento == intentos:
                break
            espera = min(2 ** intento, 20) + random.uniform(0, 1)
            print(f"[SISTEMA] Gemini no disponible (intento {intento}/{intentos}): {e}. Reintentando en {espera:.1f}s...")
            time.sleep(espera)

    raise HTTPException(
        status_code=503,
        detail="El servicio de IA (Gemini) no está disponible en este momento. Intenta de nuevo en unos minutos.",
    ) from ultimo_error


def generar_contrato_gemini(prompt: str, feedback: Optional[str] = None) -> str:
    mensaje = f"Solicitud del usuario: {prompt}"
    if feedback:
        mensaje += f"\n\nTu intento anterior falló. Corrígelo:\n{feedback}"
    return _llamar_gemini(f"{SYSTEM_PROMPT}\n\n{mensaje}")


def crear_paquete_temporal(pkg_name: str, lib_code: str, test_code: str) -> Path:
    pkg_dir = CONTRACTS_DIR / pkg_name
    src_dir = pkg_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Salvaguarda: si el modelo no respetó la regla de anteponer `#![cfg(test)]`,
    # sin esto `stellar contract build` falla al compilar test.rs en el build de
    # release de WASM (donde `testutils` no existe), aunque `cargo test` sí pase.
    if not test_code.lstrip().startswith("#![cfg(test)]"):
        test_code = "#![cfg(test)]\n" + test_code

    cargo_toml = f"""[package]
name = "{pkg_name}"
version = "0.0.0"
edition = "2021"
publish = false

[lib]
crate-type = ["lib", "cdylib"]
doctest = false

[dependencies]
soroban-sdk = {{ workspace = true }}

[dev-dependencies]
soroban-sdk = {{ workspace = true, features = ["testutils"] }}
"""
    (pkg_dir / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    (src_dir / "lib.rs").write_text(lib_code, encoding="utf-8")
    (src_dir / "test.rs").write_text(test_code, encoding="utf-8")
    return pkg_dir


def eliminar_paquete_temporal(pkg_dir: Path):
    shutil.rmtree(pkg_dir, ignore_errors=True)


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise HTTPException(
            status_code=504,
            detail=f"El comando '{' '.join(cmd)}' excedió el límite de {timeout}s.",
        ) from e


def generar_y_validar_contrato(prompt: str, pkg_name: str) -> tuple[Path, str, str, list[dict]]:
    """Loop de autorreparación: genera, prueba y compila hasta 3 veces, reenviando
    el error del compilador al modelo en cada reintento."""
    feedback: Optional[str] = None
    ultimo_lib = ""
    ultimo_test = ""
    ultimo_error = "Error desconocido."
    pkg_dir = CONTRACTS_DIR / pkg_name

    for intento in range(1, MAX_GENERATION_ATTEMPTS + 1):
        print(f"\n[SISTEMA] Intento {intento}/{MAX_GENERATION_ATTEMPTS} de generación...")
        try:
            raw = generar_contrato_gemini(prompt, feedback)
            lib_code, test_code, functions = parsear_respuesta_gemini(raw)
        except ValueError as e:
            ultimo_error = str(e)
            feedback = (
                f"Tu respuesta no siguió el formato exacto pedido: {ultimo_error}\n"
                "Devuelve EXACTAMENTE los tres bloques ### LIB, ### TEST y ### FUNCTIONS."
            )
            continue

        ultimo_lib, ultimo_test = lib_code, test_code
        crear_paquete_temporal(pkg_name, lib_code, test_code)

        test_result = _run(["cargo", "test", "-p", pkg_name], cwd=BASE_DIR, timeout=CARGO_TEST_TIMEOUT)
        if test_result.returncode != 0:
            ultimo_error = (test_result.stderr or test_result.stdout)[-4000:]
            feedback = (
                "El código no compiló o los tests fallaron. Este es el error del compilador:\n"
                f"{ultimo_error}\n"
                "Corrige lib.rs y test.rs y devuelve de nuevo los tres bloques completos."
            )
            print(f"[SISTEMA] cargo test falló en el intento {intento}.")
            continue

        build_result = _run(
            ["stellar", "contract", "build", "--package", pkg_name],
            cwd=BASE_DIR,
            timeout=BUILD_TIMEOUT,
        )
        if build_result.returncode != 0:
            ultimo_error = (build_result.stderr or build_result.stdout)[-4000:]
            feedback = (
                "cargo test pasó pero 'stellar contract build' falló con este error:\n"
                f"{ultimo_error}\n"
                "Corrige lib.rs y test.rs y devuelve de nuevo los tres bloques completos."
            )
            print(f"[SISTEMA] stellar contract build falló en el intento {intento}.")
            continue

        print(f"[SISTEMA] Contrato válido tras {intento} intento(s).")
        return pkg_dir, lib_code, test_code, functions

    eliminar_paquete_temporal(pkg_dir)
    raise HTTPException(
        status_code=400,
        detail={
            "error": f"No se pudo generar un contrato válido tras {MAX_GENERATION_ATTEMPTS} intentos.",
            "last_compiler_error": ultimo_error,
            "last_lib_code": ultimo_lib,
            "last_test_code": ultimo_test,
        },
    )


def auditar_contrato(lib_code: str) -> list[dict]:
    try:
        texto = _llamar_gemini(
            f"{SYSTEM_PROMPT_AUDIT}\n\nCódigo del contrato:\n```rust\n{lib_code}\n```",
            intentos=2,
        ).strip()
        match = re.search(r"\[.*\]", texto, re.DOTALL)
        if not match:
            return [{"severity": "low", "description": "No se pudo parsear la auditoría automática.", "location": "n/a"}]
        hallazgos = json.loads(match.group(0))
        return hallazgos if isinstance(hallazgos, list) else []
    except Exception as e:
        print(f"[ADVERTENCIA] La auditoría automática falló: {e}")
        return [{"severity": "low", "description": f"La auditoría automática falló: {e}", "location": "n/a"}]


def invocar_funcion_contrato(contract_id: str, function_name: str, args: dict, functions: list[dict]) -> str:
    manifest = next((f for f in functions if f.get("name") == function_name), None)
    if manifest is None:
        raise HTTPException(status_code=400, detail=f"La función '{function_name}' no está en el manifiesto del contrato.")

    manifest_arg_names = {a["name"] for a in manifest.get("args", [])}
    for arg_name in args:
        if arg_name not in manifest_arg_names:
            raise HTTPException(status_code=400, detail=f"Argumento desconocido: '{arg_name}'.")

    cmd = [
        "stellar", "contract", "invoke",
        "--id", contract_id,
        "--network", "testnet",
        "--source-account", STELLAR_SOURCE_IDENTITY,
        "--",
        function_name,
    ]
    for arg in manifest.get("args", []):
        name = arg["name"]
        if name in args:
            cmd.extend([f"--{name}", args[name]])

    result = _run(cmd, cwd=BASE_DIR, timeout=INVOKE_TIMEOUT)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Falló la invocación: {result.stderr or result.stdout}")
    return result.stdout.strip()


@app.post("/generate-and-deploy")
def generate_and_deploy(req: ContractRequest, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")

    pkg_name = f"gen-{uuid.uuid4().hex[:8]}"

    try:
        pkg_dir, lib_code, test_code, functions = generar_y_validar_contrato(req.prompt, pkg_name)

        security_findings = auditar_contrato(lib_code)

        wasm_filename = pkg_name.replace("-", "_") + ".wasm"
        wasm_path = WASM_DIR / wasm_filename
        if not wasm_path.exists():
            raise HTTPException(status_code=500, detail=f"No se encontró el WASM esperado en {wasm_path}")

        print(f"\n[SISTEMA] Desplegando '{pkg_name}' desde: {wasm_path}")
        deploy_result = _run(
            [
                "stellar", "contract", "deploy",
                "--wasm", str(wasm_path),
                "--source", STELLAR_SOURCE_IDENTITY,
                "--network", "testnet",
            ],
            cwd=BASE_DIR,
            timeout=DEPLOY_TIMEOUT,
        )

        if deploy_result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Falló stellar deploy: {deploy_result.stderr}")

        salida = deploy_result.stdout
        contract_id_match = re.search(r"C[A-Z0-9]{55}", salida)
        contract_id = contract_id_match.group(0) if contract_id_match else None
        if not contract_id:
            raise HTTPException(status_code=500, detail="No se pudo extraer el contract_id de la salida de deploy.")

        with _contract_lock:
            _contract_functions[contract_id] = functions

        # Si el usuario conectó Freighter y el contrato tiene una función
        # 'initialize' cuyos argumentos son todos tipo address, aplicamos su
        # dirección como owner automáticamente. Si 'initialize' pide además
        # otros datos (montos, flags, etc.) no los inventamos: el usuario los
        # completa a mano en el panel de funciones.
        owner_applied = False
        if req.owner_address:
            initialize_fn = next((f for f in functions if f.get("name") == "initialize"), None)
            if initialize_fn:
                args_spec = initialize_fn.get("args", [])
                if args_spec and all(a.get("type") == "address" for a in args_spec):
                    try:
                        invocar_funcion_contrato(
                            contract_id,
                            "initialize",
                            {a["name"]: req.owner_address for a in args_spec},
                            functions,
                        )
                        owner_applied = True
                        print(f"[SISTEMA] Owner {req.owner_address} aplicado vía initialize().")
                    except HTTPException as e:
                        print(f"[ADVERTENCIA] No se pudo auto-inicializar con el owner: {e.detail}")

        return {
            "status": "success",
            "contract_id": contract_id,
            "rust_code": lib_code,
            "test_code": test_code,
            "functions": functions,
            "security_findings": security_findings,
            "owner_address": req.owner_address,
            "owner_applied": owner_applied,
            "explorer_url": f"https://stellar.expert/explorer/testnet/contract/{contract_id}",
        }

    except HTTPException as he:
        print(f"\n[HTTP EXCEPTION LANZADA]: {he.detail}")
        raise he
    except Exception as ex:
        print("\n--- ERROR CRÍTICO NO CONTROLADO ---")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error interno crítico: {str(ex)}")


@app.post("/invoke")
def invoke_contract(req: InvokeRequest, request: Request):
    check_rate_limit(request.client.host if request.client else "unknown")

    with _contract_lock:
        functions = _contract_functions.get(req.contract_id)
    if functions is None:
        raise HTTPException(status_code=404, detail="Contrato desconocido (no fue desplegado en esta sesión).")

    resultado = invocar_funcion_contrato(req.contract_id, req.function, req.args, functions)
    return {"status": "success", "function": req.function, "result": resultado}
