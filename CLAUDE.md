# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Prompt2Contract: a hackathon MVP that turns a natural-language prompt into a deployed Stellar Soroban smart contract. A FastAPI backend (`mi_contrato_base/main.py`) calls Gemini to generate Rust contract code, writes it over the existing `hello-world` contract, builds it with the Stellar CLI, and deploys it to testnet. `mi_contrato_base/index.html` is a static single-page frontend (no build step, no framework) that posts prompts to the backend and shows the deployed contract ID / explorer link, with optional Freighter wallet connection.

All real project content lives under `mi_contrato_base/`.

## Running the app

Backend (from `mi_contrato_base/`):
```sh
python -m venv .venv && .venv\Scripts\activate  # if not already set up
pip install fastapi uvicorn google-genai
uvicorn main:app --reload
```
The backend listens on `http://127.0.0.1:8000` — `index.html` is hardcoded to call that exact origin, so run it there. Open `index.html` directly in a browser (no dev server needed).

On startup, `main.py` auto-provisions a Stellar testnet identity named `alice` via the `stellar` CLI (`stellar keys generate`/`fund`) if one doesn't already exist.

**Known issues in `main.py` worth flagging if touched:**
- `client = genai.Client(api_key="AQUI VA LA API")` — placeholder key, not read from env/config.
- The lib.rs write path and wasm/deploy paths are hardcoded to `C:\Stellar\traductor_stellar\mi_contrato_base\...`, not derived from the actual repo location.

## Build / test (Soroban contract workspace)

The contract workspace root is `mi_contrato_base/` (has its own `Cargo.toml`, `AGENTS.md`). From there:

```sh
stellar contract build              # builds every cdylib member to WASM
stellar contract build --package <name>   # build one crate
cargo test                          # host-side unit tests (not on-chain)
cargo test -p <name>                # single crate
```

- Do **not** substitute `stellar contract build` with `cargo build --target wasm32v1-none` — the Stellar CLI applies flags/metadata the network expects.
- Requires the `wasm32v1-none` target (`rustup target add wasm32v1-none`) and Rust ≥ 1.84 (1.82/1.83 cannot build contracts).
- WASM artifacts land in `mi_contrato_base/target/wasm32v1-none/release/*.wasm`.

Deploy/invoke on testnet:
```sh
stellar contract deploy --wasm target/wasm32v1-none/release/<name>.wasm --source-account <identity> --network testnet --alias <alias>
stellar contract invoke --id <alias> --network testnet --source-account <identity> -- <function> --arg value
```

## Architecture

- `mi_contrato_base/contracts/hello-world/` — the single Soroban contract crate. `src/lib.rs` is **overwritten at runtime** by the backend's `/generate-and-deploy` endpoint each time a user submits a prompt, so its current content reflects the last AI-generated contract, not a fixed hand-written one. `src/test.rs` holds host-side tests for whatever contract is currently in `lib.rs`.
- `mi_contrato_base/main.py` — single-file FastAPI backend with one endpoint, `POST /generate-and-deploy`, that does: call Gemini with a strict system prompt (Rust-only output) → extract the ` ```rust ` block → overwrite `lib.rs` → `stellar contract build` → `stellar contract deploy --source alice` → parse the contract ID from deploy stdout → return `{status, contract_id, rust_code, explorer_url}`.
- `mi_contrato_base/index.html` — plain HTML/CSS/JS (no bundler). Talks to the backend via `fetch`, keeps a client-side (non-persisted) deployment history list, and optionally connects a Freighter wallet via `@stellar/freighter-api` (loaded from unpkg).
- New/generated contracts are Soroban contracts using `soroban-sdk = "27"` (pinned in the workspace `Cargo.toml`), `#![no_std]`, `#[contract]`/`#[contractimpl]`/`#[contracttype]`/`#[contracterror]` macros — this is the shape the Gemini system prompt in `main.py` enforces for any AI-generated contract.
