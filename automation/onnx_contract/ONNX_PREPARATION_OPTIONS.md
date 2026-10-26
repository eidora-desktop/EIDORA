# Global preparation options — schema 3, implementation 2

The executable catalog is `EmbeddingsArea/preparation_contract.py`. Its JSON
snapshot is `onnx-contract.json`. EIDORA runs preparation in its isolated
background worker, immediately before ONNX inference. Models expose only the
parameters needed for their workflow and declare recommended defaults.

| ID | Modalities | Type / global limits | Meaning / condition |
| --- | --- | --- | --- |
| resize | image, video | stretch, fill_crop, fit_pad, short_edge_crop | Geometry before fixed-size tensor creation |
| resize_rounding | image, video | round, truncate | Integer resize dimensions; default round when omitted; active for fill_crop, fit_pad, short_edge_crop |
| interpolation | image, video | nearest, bilinear, bicubic, lanczos | Pillow RGB resize filter |
| crop_position | image, video | top_left, top, top_right, left, center, right, bottom_left, bottom, bottom_right | Active for fill_crop and short_edge_crop; default center |
| short_edge | image, video | integer 1–16,384 | Active for short_edge_crop; must be at least max(final width, final height) |
| alpha_background | image, video | three integers 0–255 | Transparency composite and padding color; default [0,0,0] |
| truncation_side | text | left, right | Side from which excess tokens are removed |
| prefix, suffix | text | literal strings | Applied before tokenization; spaces preserved; default empty |
| channels | audio | mono or integer 0–255 | Layout-aware mono downmix or zero-based channel selection |
| excerpt | audio, video | beginning, center, end, custom | Select one excerpt; default beginning |
| start_seconds | audio, video | number 0–604,800 | Active for custom excerpt; default 0 |
| duration_seconds | video | number 0.001–86,400 | Excerpt duration, retaining fixed model frame count |

Numeric widgets retain up to twelve fractional decimals for temporal values.
Narrow ranges to useful model-specific values. Invalid channels, starts beyond media, and oversized intermediate images
fail preparation rather than changing meaning silently.

## Spatial semantics

Stretch directly resizes to W×H. Fill/crop preserves aspect, scales by
max(W/w,H/h), then crops. Fit/pad scales by min(W/w,H/h) and centers the image
on a background canvas. Short-edge/crop scales by short_edge/min(w,h), then crops
to the independent final W×H. `preparation.resize_rounding` defaults to `round`
(Python round, ties to even). Explicit `truncate` discards fractional pixels
using exact integer ratios, avoiding floating-point errors at integer boundaries.
Fit/pad retains a minimum of one pixel on each axis. Stretch always uses the
fixed W×H and ignores this setting. Crop positions still use 0,
floor(remainder/2), or remainder on each axis, independently of resize rounding.
An intermediate float-image footprint above 256 MiB is rejected.

For a reference pipeline that truncates resize dimensions, declare:

```yaml
preparation:
  # Other required preparation fields remain unchanged.
  resize_rounding: truncate
```

For example, 640×480 with `short_edge: 224` becomes 298×224 with truncation,
instead of the default 299×224. Packages without the field retain round behavior.
Compare the complete prepared tensors against the reference model with matching
Pillow versions; this setting changes only resize dimensions.

Image preparation: first page/frame → EXIF orientation → alpha composite/RGB →
geometry/interpolation → float32 `(pixel*rescale-mean)/std` → layout → batch.
Video uses the same spatial recipe consistently for all selected frames.

## Text and temporal semantics

Text: read UTF-8 → literal prefix/suffix → packaged tokenizer.json → configured
special tokens, truncation and fixed padding → named tensors. No chat templates,
arbitrary Python tokenizers or automatic chunking. Padding uses tokenizer_config.json's
pad_token, otherwise [PAD], with ID 0 if unavailable.

Audio: decode first stream continuously from its beginning → choose channel or
mono downmix → PyAV float planar resampling → select the exact target-rate sample
interval → right-zero-pad → waveform plus optional valid count/mask. Continuous
decoding avoids compressed-frame seek shifts and retains only the output excerpt.
Late custom excerpts can therefore take longer to decode. Sample positions use
round(start_seconds*sample_rate); original container timing offsets are not a
separate alignment interface.

Video: select first visual stream → choose excerpt → sample target timestamps
start+k*duration/frame_count, excluding the right endpoint → first decoded frame
at or after each target → spatial preparation → repeat last available frame at
EOF → fixed layout. No decoded frame is an error. Center/end need reliable
positive duration. Custom starts must be nonnegative and before EOF. Video
rotation metadata, HDR tone mapping and broken timestamps remain decoder limits.

## Fixed boundaries and excluded workflows

Final image size, tokenizer, token length, audio sample rate/count, video frame
count, dtype/layout and normalization remain package requirements. Model-specific
feature extraction and pooling belong inside ONNX. A graph with embedded input
normalization must declare identity external normalization to prevent duplication.

One input produces one vector per item. Multiple crops/clips, document chunking,
aggregation, random augmentation, arbitrary filters, runtime resolution selection
and executable hooks are outside this version. Alternative preparation choices
are shape-compatible experiments, not claims of equivalent model quality.
