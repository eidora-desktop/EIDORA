# Runtime adapters

`base.py` defines setup, batch embedding, and cleanup contracts. `registry.py`
registers adapters; `onnx.py` is the single image/text/audio/video executor and
`preparers.py` owns the small modality-specific tensor preparations. `fake.py` is a
deterministic development backend, not a model for production embeddings.

Return a two-dimensional float32 matrix aligned with input order. Honor the
strict schema-v3 effective preparation contract. Auto prefers declared and installed
CUDA, then CoreML, then CPU within the platform policy: macOS permits CoreML only
on Apple Silicon and permits CPU only on Intel Macs. The parent process owns one
full-run CPU retry after accelerator setup/inference failure, preserving truthful
attempt provenance.

CoreML sessions explicitly use `ModelFormat=MLProgram` through ONNX Runtime's
provider-options API (1.20 or later). The legacy NeuralNetwork
format can round normalized float32 embeddings enough to fail the output contract.
Keep normalization and finite-value checks strict; do not repair or renormalize
model output in the adapter. The packaged audit uses the same provider configuration
and a normalized convolution/pooling graph to catch this precision regression.

ONNX Runtime also omits a required MLProgram `pad` input for some valid default
padding ([upstream issue](https://github.com/microsoft/onnxruntime/issues/33574)).
Only this compiler error triggers one CoreML setup retry via `runtime/coreml.py`:
make default zero padding explicit in a temporary graph and preserve model math.
Resolve external tensors into temporary weights and remove these files after
session construction, including on failure. Never rewrite the downloaded package
or its checksums. CPU/CUDA sessions do not import ONNX or take this retry path.

Before CoreML compilation, validate the original graph's
contract using CPU session metadata, then configure CoreML with its named batch
dimensions fixed to one, other named dimensions resolved from the input contract,
and `MLComputeUnits=CPUAndGPU`. Set `RequireStaticInputShapes=1` so unresolved
intermediate shapes stay on the CPU partition instead of reaching Apple’s compiler. Split larger requests
into ordered single-item inference calls using the existing batch limiter.
This may reduce throughput, but does not alter saved contracts, model files,
or output validation. This policy uses graph dimensions, never model names, and avoids dynamic
compilation paths that can abort in native Metal code before Python can retry. Other unsupported graphs still fail normally.

Keep heavyweight runtime imports inside execution paths. Update adapter and
preprocessing tests when adding a backend or changing model inputs.
