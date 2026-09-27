FROM python:3.12-slim

# El backend compila y despliega contratos Soroban en cada request, así que la
# imagen necesita Rust + el target wasm32v1-none + Stellar CLI, no solo Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential pkg-config libssl-dev libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:/usr/local/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable \
    && rustup target add wasm32v1-none

# Binario precompilado oficial: mucho más rápido y sin las dependencias nativas
# (libdbus/libudev) que pide compilar stellar-cli desde cero con `cargo install`.
RUN curl -fsSL https://raw.githubusercontent.com/stellar/stellar-cli/main/install.sh | sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
