# Testing the NPU: LLM inference on the unlocked CDSP

Companion to [RESEARCH_NOTES.md](RESEARCH_NOTES.md). The unlock gets the
Hexagon to `running`; this covers what happens after that: how to run an LLM
on it, what speed to expect from each backend (NPU hybrid, CPU, GPU), the
hard limits, and the failures that cost us device time.

Everything below was measured on one Retroid Pocket 6 (QCM8550-class SoC,
Hexagon HTP **v73**, 11.5 GB RAM), firmware QCM8550.LA.1.0-00035-STD.PROD-2 /
CDSP.HT.2.8-00906-KAILUA-1, across 2026-08-26 to 2026-08-28. Same disclaimer
as the README: one device, one firmware, one person. These are case study
numbers.

## TL;DR

| Question | Answer (measured) |
|---|---|
| What is the NPU actually good for? | **Prefill and long-context ingestion**. 2.5-7x the best CPU rate, and the rate barely moves with core config: anything that can drive the orchestration gets the same t/s |
| Can it decode alone? | No. The working runtime is a **hybrid**: HTP does the big matmuls, CPU cores orchestrate. Decode speed tracks the CPU cores, not the NPU |
| Who decodes fastest? | The **CPU**, at an equal core budget (0.8B: ~34 t/s on three mid cores vs 16.5 on the hybrid). Hybrid decode trades throughput for TTFT and a small CPU footprint |
| GPU / OpenCL? | Out. OpenCL decodes a 0.8B slightly faster than the hybrid (17.2 t/s), but prefill is 2.5x slower than the NPU and model load pins ~4.3 GB of RAM, which killed the game client in every coexistence test. Vulkan does not work at all on the Adreno driver |
| Biggest model that fits | **~4 GB mapped per session.** Hexagon VA space is 32-bit. A 9B runs at 75% layer offload: 5.1 t/s |
| Worst failure mode | Unsatisfiable anonymous-DMA at model load: kernel panic, full reboot, no LMK rescue. Pre-flight a memory guard before every load |

## 1. The runtime: GenieX `llama_cpp` hybrid (no root needed)

The only path we found that runs ordinary GGUF files on the HTP of this
device is the **GenieX v0.5.0 `llama_cpp` plugin** (a Qualcomm llama.cpp fork
carrying `libggml-hexagon.so` plus per-arch HTP skels such as
`libggml-htp-v73.so`). No root, no signing: unsigned-PD sessions work once
the CDSP remoteproc is up.

Two details decide whether it works:

1. **The plugin clobbers `ADSP_LIBRARY_PATH`.** At plugin init it overwrites
   the variable with its own lib root, but the HTP skels ship one directory
   deeper (`lib/llama_cpp/`), so every session open fails with
   `0x80000406 ... No such file or directory`. A lot of "NPU looks
   firmware-locked" logs are this bug. Fix: copy the skels up one level,
   `cp lib/llama_cpp/libggml-htp-*.so lib/` (same inside an app sandbox:
   copy into `nativeLibraryDir`).
2. **`/vendor/lib64` must be on `LD_LIBRARY_PATH`** for `libcdsprpc.so` and
   OpenCL when running from an app sandbox (`run-as`).

Working invocation pattern (from an app sandbox via `run-as`; from a root
shell the same minus `run-as`):

```bash
run-as <your.app> sh -c 'cd files/gx && \
  LD_LIBRARY_PATH=$PWD/lib:$PWD/lib/llama_cpp:$PWD/lib/qairt:/vendor/lib64:/system/lib64 \
  ./bin/geniex-bench --plugin llama_cpp --device npu \
  -c 8192 -p 512 -n 128 -r 1 -m /path/to/model-Q4_0.gguf'
```

- `--device npu` really means **hybrid**: an HTP session plus a ~1 GB CPU
  compute buffer. The NPU part of the work is concentrated in prefill
  (see §3).
- CPU pinning: `taskset <decimal-mask>` *inside* the `sh -c`, before the
  binary (toybox rejects `0x`). Cores on this SoC: `7` = the three A510
  littles (cpu0-2), `56` = three mids (cpu3-5), `120` = four mids (cpu3-6),
  `16` = one mid (cpu4), `128` = the prime (cpu7).
- `libggml-htp-v73.so` exists because the skel is per-HTP-version; v75/v79/
  v81 ship alongside for other chips. If your DSP reports a different arch,
  that is the file to check first.

A separate, older **pure-QNN path** (Qualcomm Genie plus precompiled W4A16
context binaries, e.g. the `imi2/QNN-HTP-LLM-Genie-models` Llama-3.2-3B
bundle) also works on v73: 530 t/s prefill, ~9 t/s decode on Llama-3.2-3B.
It needs per-model precompiled bundles though, not GGUFs.

## 2. Model format rules (HTP path)

- **Use Q4_0.** The HTP path is Q4_0-native. K-quants load and run but are
  ~3.5x slower in prefill (Qwen3.5-2B: Q4_K_M 61 t/s PP vs Q4_0 216 t/s;
  TG 6.3 vs 9.1). Q8_0 works and decodes nearly as fast on tiny models.
- **Gemma-4 E2B Q8_0 crashes the loader** (FORTIFY size underflow at load;
  its Q4_0 and Qwen Q8_0 both load fine). Untested elsewhere; treat E2B as
  Q4_0-only on this stack.
- **Stock Qwen3.5 thinks by default** when no chat template is applied.
  Thinking tokens are decode tokens, i.e. hidden seconds at these speeds.
  Disable via `enable_thinking=false` (template) or a pre-closed empty
  `<think></think>` in raw mode.
- Prefill rate depends on prompt length (fixed startup amortizes over more
  tokens). Compare like with like: 4B PP is ~70 t/s at 128-token prompts but
  ~135 t/s at 512+.

## 3. Speed results

All runs: geniex v0.5.0 `llama_cpp --device npu` (hybrid), ctx 8192, KPIs
from the tool itself.

| model | quant | cores | PP t/s | TG t/s |
|---|---|---|---|---|
| Qwen3.5-0.8B (tuned) | Q4_0 | all 8 | 448 | **22.3** |
| Qwen3.5-0.8B (tuned) | Q4_0 | 3×A510 | 447 | 19.5 |
| Qwen3.5-0.8B (tuned) | Q4_0 | 1×A510 | 438 | 5.3 |
| Qwen3.5-0.8B (tuned) | Q8_0 | all 8 | 370 | 12.3 |
| Qwen3.5-0.8B (tuned) | Q8_0 | 1×A510 | 432 | 11.3 |
| Gemma-4-E2B (tuned) | Q4_0 | all 8 | 452 | 11.4 |
| Gemma-4-E2B (tuned) | Q4_0 | 3×A510 | 417 | 10.9 |
| Qwen3.5-2B (stock) | Q4_0 | all 8 | 216 | 9.1 |
| Qwen3.5-4B (stock) | Q4_0 | all 8 | 131.6 | 7.6 |
| Qwen3.5-4B (stock) | Q4_0 | 3×mid | 135.0 | 6.1 |
| Qwen3.5-4B (stock) | Q4_0 | 1×mid | 136.4 | ~2.7-5 |
| Qwen3.5-4B (stock) | Q4_0 | 3×A510 | 135.5 | 6.1 |
| Qwen3.5-9B (stock) | Q4_0 | `-ngl 24` (~4.0 GB HTP) | 48.5 | 5.1 |
| Qwen3.5-9B (stock) | Q4_0 | `-ngl 16` (~2.7 GB HTP) | 34.8 | n/a |
| Qwen3.5-9B (stock) | Q4_0 | all layers | **load fails** (§5) | n/a |
| Llama-3.2-3B (pure QNN, ref.) | W4A16 | n/a | 530 | 9.0 |

PP numbers are at 512-token prompts (0.8B/E2B rows: 128-token) after warmup.

**Patterns that held in every run:**

- **PP is HTP-bound and core-invariant.** 0.8B/E2B: ~430-450 t/s in every
  config; 4B: 131-137 t/s in every config; identical within 1-4%. The CPU
  cores are near-idle during prefill. Long RP-style context turns cost
  almost nothing on the NPU.
- **TG scales with CPU cores** (the hybrid CPU share), from 2.7 t/s on one
  prime to 22 t/s on eight cores for the 0.8B. The NPU does not rescue
  decode.
- **On the hybrid path, decode depends on core count, not core speed.**
  3×mid and 3×A510 both measure exactly 6.1 t/s on the 4B, and 2×mid (5.5)
  is within noise of 2×A510 (5.3). The per-token loop is bounded by the
  HTP-CPU handshake, so faster cores just reach the wait sooner. The
  pure-CPU path shows the normal ~3.5x mid-over-little gap, so this is a
  hybrid property, not a device quirk.
- The 9B hybrid decodes *faster* than the 4B full offload (5.1 vs 4.8): the
  hybrid split scales better with dense model size than the all-NPU curve.

## 4. Backend vs backend: NPU vs CPU vs GPU

Same model (Qwen3.5-0.8B tuned Q4_0), same prompt shapes, same session:

| backend | prefill t/s | decode t/s |
|---|---|---|
| **Hybrid HTP** (`--device npu`), 3 mid cores | **537** | 16.5 |
| Hybrid HTP, 1 mid core | 543 | 8.1 |
| KleidiAI CPU (llama.cpp), 3 mid cores | 164† | **34**† |
| KleidiAI CPU, 1 mid core | ~72† | ~17† |
| Adreno OpenCL (`--device gpu`) | 220 | 17.2 |
| Vulkan | broken | broken |

† from the historic server-side CPU matrix (0.8B Q4_0, mid 3-5, KleidiAI +
repack). The same-day CPU cells measured lower through a client-side
bottleneck, so treat CPU numbers as a range: prefill ~72-164, decode ~17-34
at these core budgets.

- **Prefill: the NPU wins everywhere.** 2.5-7x the best CPU number at every
  core budget (0.8B: 537 vs 164; E2B: 452 vs 68; 2B: 216 vs 85), and it
  costs the same regardless of which cores drive it. This is what makes the
  NPU worth having: a 1k-token history turn prefills in ~1.9 s on the NPU
  vs ~6-12 s on the CPU.
- **Decode: the CPU wins at an equal core budget**, at every model size we
  tried (0.8B: 34 vs 16.5; E2B: 15.5-18.7 CPU vs 11.4 hybrid; 2B: 16.4 vs
  9.1). The hybrid's per-token CPU-DSP handshake costs more than it returns
  once the model is small enough for the cores to keep busy. Use hybrid
  decode when you need the CPU budget back, or for models too big for
  comfortable CPU decode.
- **GPU: dismissed.** OpenCL works and even posts the fastest single decode
  number (17.2 t/s on the 0.8B). But its prefill is 2.5x slower than the
  NPU, and the bigger problem is memory: OpenCL model load pins ~4.3 GB of
  system RAM for a 1.26 GB model, and every coexistence attempt with a game
  client ended in an lmkd cascade that killed the game. Vulkan is dead
  outright: the proprietary Adreno 740 driver fails compute-pipeline
  creation on some shaders and silently produces garbage on the rest.

## 5. Memory: the ceiling and the cliff

### The ceiling: ~4 GB mapped per session

The Hexagon Q6 is a 32-bit ISA: each protected session gets a **32-bit VA
space of about 4 GiB** minus shell carve-outs. Two details make it worse:

- Mappings are **cumulative and not reclaimed on `munmap`**. A DSP VA slot
  is only released when the underlying buffer is freed. Window-fill failures
  hit the *first new allocation* after exhaustion, not the big one.
- Practical limit we hit: all-layers 9B (5.38 GB into HTP buffers) fails
  deterministically with `fastrpc_mmap failed error 0x00000001` on a
  trailing 50 MB buffer; a 4.0 GB mapping succeeds. Community numbers agree
  (qualcomm/fastrpc issue #137, filed from *another* SD 8 Gen 2 phone and
  closed as not planned; ggml-hexagon discussion #18 cites "~3.5 GB" per
  session as the biggest technical challenge).

**Workaround: partial offload with `-ngl`.** 9B at `-ngl 24` (75% of layers,
~4.0 GB) loads and decodes 5.1 t/s. A 4B (2.9 GB) fits whole.

KV cache shares this window: Qwen3.5-4B keeps **16 KiB/token** at f16 (8
full attention layers plus 24 linear-attention/GDN layers), so 32k context
adds 512 MiB of KV *inside* the HTP mapping. Gemma-4-E2B's sliding-window
attention keeps it at 4.3 KiB/token: KV is a non-issue there.

### The cliff: unsatisfiable DMA = kernel panic

The geniex loader reads weights into **anonymous private memory for DSP
DMA** (nothing is mmap-reclaimable, unlike the CPU path). When such an
allocation is unsatisfiable, the kernel panics: full device reboot, no lmkd
rescue, no app-level crash.

| MemAvailable at load | result |
|---|---|
| ~2.9 GB | E2B load reboots the device |
| ~2.1 GB / ~1.4 GB | 0.8B loads fine, zero speed loss, PSI ≤ 5% |
| ~0.1 GB | 0.8B load crashes the device |

Loaded-model footprints (app-uid smaps, mid-generation): 0.8B Q8_0
660 MB Pss / 896 MB peak; 0.8B Q4_0 541/715 MB; 2B Q4_0 1.01/1.39 GB;
4B Q4_0 1.30/1.80 GB; E2B Q4_0 2.67/2.99 GB.

**Hard rule for any runtime: pre-flight a MemAvailable guard before every
load** (refuse below model peak + ~0.7 GB margin). This is the only failure
in this document that reboots the whole device.

### KV quantization (CPU path only, measured)

q8_0 KV is quality-neutral on Qwen3.5-4B at 27.6k context (needle battery
3/3 vs f16 3/3, greedy, temp 0; consistent with earlier 8-32k batteries) and
halves KV RAM (64 MiB saved @8k, 256 MiB @32k). Exposed as
`-ctk q8_0 -ctv q8_0` on the llama.cpp **CPU** path; **geniex v0.5.0 has no
KV flags**, so hybrid NPU runs use f16 KV. Respect published Gemma-4
KV-quant degradation reports for E2B long-context; we did not reproduce
them at ≤8k.

## 6. Gotchas checklist (each cost us a run)

1. **Never `kill -9` a hybrid bench mid-load.** The killed process's HTP
   session leaks; every subsequent bench wedges at session-open (busy-spin,
   fastrpc ioctl retries in dmesg). Only a **reboot** clears it.
2. geniex synthetic random-id prompts make the model sample EOS first, so
   you get `decode=0.0 gen=0` with a healthy load. Real text prompts
   required.
3. `--prompt-file` at very large `-c` can ingest only the prompt *tail*
   (we saw ~38 of 27.6k tokens processed). Don't trust on-device
   long-context quality tests through that path.
4. Old llama.cpp builds: `/health` returns 200 *during* model load (gate on
   a real generated token instead), and SSE streams drop content deltas
   (use blocking completions).
5. Batteries/keepalive: wireless adb mDNS sleeps; poll the connection or it
   dies mid-run. A `timeout`-wrapped command can't exec a shell *function*,
   so wrap in a real executable.
6. `kill $VAR` with VAR unset kills your own runner script's process group.
   Device-side zombies need killing via `run-as` (shell uid gets EPERM).

## 7. Deployment picks on a handheld (RP6-shaped)

| Scenario | Pick | What you get |
|---|---|---|
| Game running (CPU must stay free) | 0.8B Q8_0 hybrid pinned to 1×A510 or 1×mid | 11.3 t/s decode, 432 t/s prefill, single-core CPU footprint |
| Balanced bot voice | 0.8B Q4_0 hybrid on 3×mid | 19.5-22 t/s decode |
| Fastest decode, game closed | KleidiAI CPU on 3 mid cores | 0.8B Q4_0: 34 t/s decode |
| Quality-first, game closed | Qwen3.5-4B Q4_0 hybrid, 3×mid | 6.1 t/s decode, 135 t/s prefill |
| Big-model party trick | 9B Q4_0 `-ngl 24` | 5.1 t/s at the 4 GB ceiling |
| Anything GPU | none | Out: slower prefill, tiny-model-only decode win, RAM pinning kills coexistence |

## 8. Open items (worked around, not solved)

- **Reliable power numbers.** We measured whole-device watts during all of
  the above, but the instrumentation is hostile (the fuel gauge reports
  idle-level current while the panel sleeps, charge-current semantics on
  USB, short single-shot windows) and the numbers did not reproduce tightly
  enough to publish. Speed claims above are solid; treat any watt figure
  from this device as suspect until it is re-measured with long,
  awake-gated, unplugged windows.
- One irreproducible instant-failure cell (4B TG on 4×mid, rc=1, empty log).
  Neighboring configs measured fine; cause unknown.
- geniex Gemma-4 Q8_0 loader FORTIFY crash, not yet reported upstream.
- Live LLM-while-gaming coexistence trial on the hybrid path (the CPU path
  is proven coexistence-safe; the hybrid's anon-for-DMA footprint moves the
  risk to the load guard in §5).
- 2B nothink fine-tune arm, and device-side 32k needle testing (needs the
  llama-server path, per gotcha #3).
