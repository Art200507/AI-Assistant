# llama.cpp on Raspberry Pi — Build, Tuning, and Benchmarks

Sources: ohyaan.github.io build/tuning guide; web search results on 2026
Raspberry Pi 4/5 LLM benchmarks (turingpi.com, sitepoint.com,
tinyweights.dev, crossshores.com).

## Building from source
```bash
sudo apt update
sudo apt install -y build-essential cmake git
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j "$(nproc)"
```

Native optimization (tunes for the exact CPU it's built on — not
portable to a different Pi):
```bash
cmake -S . -B build-native -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON
```

OpenBLAS build (speeds up prompt processing specifically, not token
generation):
```bash
sudo apt install -y libopenblas-dev
cmake -S . -B build-openblas -DCMAKE_BUILD_TYPE=Release \
  -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS
```

## Thread tuning
Don't assume more threads is always faster — test 1, 2, and 4 threads
explicitly. Memory bandwidth and background processes affect scaling
differently per system; the right thread count is empirical, not
assumed from core count.

## Context size and RAM measurement
Start with `-c 2048` and measure actual peak memory used:
```bash
/usr/bin/time -v ./build/bin/llama-cli \
  -m /srv/models/model.gguf -p "Test prompt" -n 128 -t 4 -c 2048
```

## --mlock
Locks the model in physical RAM to prevent it being swapped out. Only
use it when the full model + buffers actually fit in physical RAM, and
swap-related latency is a demonstrated problem — set a deliberate
systemd `LimitMEMLOCK` if used as a service. It reduces latency spikes
from memory pressure but does not make matrix multiplication itself
faster.

## Benchmarking
```bash
./build/bin/llama-bench \
  -m /srv/models/model.gguf -p 512 -n 128 -t 1,2,4 -r 5 -o md
```
Reports prompt-processing speed (pp) and token-generation speed (tg)
separately — OpenBLAS typically helps pp, not tg.

## Basic run
```bash
./build/bin/llama-cli \
  -m /srv/models/model.gguf -p "Your prompt" -n 128 -t 4 -c 2048
```

## Real-world benchmark numbers (2026)
- Raspberry Pi 4 (8GB): roughly ~1.02 tokens/sec measured in one
  independent test, CPU-bound — meaningfully slower than Pi 5.
- Raspberry Pi 5: roughly 4-7 tokens/sec for Q4_K_M-quantized larger
  models; 5-15 tokens/sec for ~1.5B models; 2-5 tokens/sec for ~3B
  models.
- llama.cpp can compress models to 1.5-8 bits, which is what lets 7B+
  parameter models run at all within 4-8GB of RAM — without aggressive
  quantization, a 7B model simply won't fit.
- General conclusion across multiple 2026 sources: Pi 4 8GB CAN run
  llama.cpp, but Pi 5 is the more practical minimum for genuinely
  usable response speed; Pi 4 is workable mainly for smaller models
  (1-3B) or patience-tolerant use cases, not snappy conversation.
