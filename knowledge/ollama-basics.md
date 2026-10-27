# Ollama — Running LLMs Locally

Source: github.com/ollama/ollama README.

## What it is
Ollama is an open-source tool for running large language models locally,
without needing cloud API access. It handles downloading, quantization
format handling, and serving models behind a simple local API and CLI —
supports model families including Llama, Gemma, Qwen, DeepSeek, Mistral,
and others, browsable at ollama.com/library.

## Installing on Linux (including Raspberry Pi OS, which is Debian-based)
```
curl -fsSL https://ollama.com/install.sh | sh
```
This is the standard one-line installer; manual install instructions
also exist in Ollama's docs for systems where the script doesn't apply.

## Running a model
```
ollama run <model-name>
```
Example: `ollama run gemma3` or `ollama run qwen2.5:1.5b`. If the model
isn't already downloaded, this pulls it first. Once running, you get an
interactive chat prompt directly in the terminal.

## Choosing a model size for constrained hardware (like a Raspberry Pi)
Ollama models are published in different parameter-count and
quantization variants (e.g. `qwen2.5:1.5b`, `qwen2.5:3b`, `llama3.1:8b`).
Smaller parameter counts and more aggressive quantization (lower bit
depth) use less RAM and run faster, at some cost to answer quality —
picking the right size for a given board is a real RAM/speed/quality
tradeoff, not a solved default.

## Useful commands
- `ollama list` — see downloaded models
- `ollama pull <model>` — download without running
- `ollama rm <model>` — remove a downloaded model
- `ollama ps` — see currently loaded/running models
