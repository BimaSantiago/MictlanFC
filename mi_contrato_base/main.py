import re
import traceback
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key="AQUI VA LA API")

# --- AUTOMATIZACIÓN DE LA CUENTA 'ALICE' PARA EL MVP ---
def preparar_cuenta_stellar():
    print("\n[SISTEMA] Verificando cuenta de despliegue 'alice'...")
    try:
        resultado = subprocess.run(["stellar", "keys", "ls"], capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if "alice" not in resultado.stdout:
            print("[SISTEMA] No existe 'alice'. Generando nuevas llaves...")
            subprocess.run(["stellar", "keys", "generate", "alice", "--network", "testnet"], check=True, encoding="utf-8", errors="ignore")
            
            print("[SISTEMA] Pidiendo XLM de prueba a Friendbot...")
            subprocess.run(["stellar", "keys", "fund", "alice", "--network", "testnet"], check=True, encoding="utf-8", errors="ignore")
            print("[SISTEMA] Cuenta 'alice' creada y financiada con éxito.")
        else:
            print("[SISTEMA] La cuenta 'alice' ya está configurada y lista.")
    except Exception as e:
        print(f"[ERROR] No se pudo configurar la cuenta de Stellar: {e}")

preparar_cuenta_stellar()

class ContractRequest(BaseModel):
    prompt: str

SYSTEM_PROMPT = """
Eres un experto desarrollador de Smart Contracts en Soroban (Stellar) usando Rust.
Tu tarea es convertir la solicitud en lenguaje natural del usuario en un código completo y funcional de Rust para Soroban.

REGLAS ESTRICTAS DE SALIDA:
1. Responde ÚNICAMENTE con el código de Rust dentro de un bloque markdown ```rust ... ```.
2. No agregues explicaciones, ni saludos, ni texto antes o después.
3. El código DEBE incluir `#![no_std]`, usar `soroban_sdk::{contract, contractimpl, Env, ...}`.
4. Asegúrate de que el código sea sintácticamente correcto para soroban-sdk.
"""

@app.post("/generate-and-deploy")
def generate_and_deploy(req: ContractRequest):
    try:
        # 1. Llamar a Gemini
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=f"{SYSTEM_PROMPT}\n\nSolicitud del usuario: {req.prompt}"
        )
        codigo_raw = response.text
        match = re.search(r"```rust\n(.*?)```", codigo_raw, re.DOTALL)
        codigo_rust = match.group(1) if match else codigo_raw
        print("\n--- CÓDIGO GENERADO POR IA ---")
        print(codigo_rust)

        # 2. Sobrescribir el lib.rs
        ruta_lib = r"C:\Stellar\traductor_stellar\mi_contrato_base\contracts\hello-world\src\lib.rs"
        with open(ruta_lib, "w", encoding="utf-8") as f:
            f.write(codigo_rust)

        # 3. Compilar usando Stellar CLI (se encarga de nombrar y ubicar perfectamente el .wasm)
        ruta_contrato = r"C:\Stellar\traductor_stellar\mi_contrato_base\contracts\hello-world"
        print(f"\n[SISTEMA] Compilando contrato con Stellar CLI en: {ruta_contrato}")
        
        resultado_build = subprocess.run(
            ["stellar", "contract", "build"],
            cwd=ruta_contrato,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        
        print("[STELLAR BUILD STDOUT]:", resultado_build.stdout)
        print("[STELLAR BUILD STDERR]:", resultado_build.stderr)

        if resultado_build.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Error al compilar contrato: {resultado_build.stderr}")

        # 4. Desplegar usando la ruta estándar que genera 'stellar contract build'
        wasm_path = r"C:\Stellar\traductor_stellar\mi_contrato_base\target\wasm32v1-none\release\hello_world.wasm"
        print(f"\n[SISTEMA] Desplegando archivo WASM desde: {wasm_path}")

        ruta_raiz_proyecto = r"C:\Stellar\traductor_stellar\mi_contrato_base"
        cmd_deploy = [
            "stellar", "contract", "deploy",
            "--wasm", wasm_path,
            "--source", "alice",
            "--network", "testnet"
        ]
        
        res_deploy = subprocess.run(
            cmd_deploy,
            cwd=ruta_raiz_proyecto,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )
        
        print("\n[STELLAR DEPLOY STDOUT]:", res_deploy.stdout)
        print("[STELLAR DEPLOY STDERR]:", res_deploy.stderr)

        if res_deploy.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Falló stellar deploy: {res_deploy.stderr}")

        salida = res_deploy.stdout
        contract_id_match = re.search(r"C[A-Z0-9]{55}", salida)
        contract_id = contract_id_match.group(0) if contract_id_match else "No detectado"

        return {
            "status": "success",
            "contract_id": contract_id,
            "rust_code": codigo_rust,
            "explorer_url": f"https://stellar.expert/explorer/testnet/contract/{contract_id}"
        }

    except HTTPException as he:
        print(f"\n[HTTP EXCEPTION LANZADA]: {he.detail}")
        raise he
    except Exception as ex:
        print("\n--- ERROR CRÍTICO NO CONTROLADO ---")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error interno crítico: {str(ex)}")