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

Keep heavyweight runtime imports inside execution paths. Update adapter and
preprocessing tests when adding a backend or changing model inputs.
