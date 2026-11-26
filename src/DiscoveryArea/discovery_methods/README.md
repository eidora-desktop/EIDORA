# Projection methods

`base.py` defines the algorithm contract. `methods/<id>/config.yaml` declares
method metadata, parameters and execution mode; its `method.py` implements the
algorithm. `DiscoveryArea/method_catalog.py` discovers these packages.

Return coordinate rows in input order and validate dimensionality. Keep numerical
dependencies lazy. Declare transform support only when implemented. Long-running
methods use `process_runner.py` so cancellation can terminate the child process.

When adding a method, update its config, focused method tests, and packaging
inventory together. Run the public source gate to validate dynamic method entries.
The individual algorithm folders follow this same contract.

New Projection lists **UMAP 0.6** (`umap`) first and **UMAP 0.5** (`umap_05`)
second. Both use the installed umap-learn 0.6 library; the latter explicitly
selects its 0.5 compatibility pipeline, not a separate 0.5 installation.
The YAML schemas contain all 52 constructor parameters for 0.6 and all 39 for
0.5.12. Defaults match those library versions, except that the 0.6 entry fixes
`compatibility_layout=False` to select the new pipeline. `init=auto` and
`optimizer=None` then resolve to recursive initialization and Adam. The 0.5
entry defaults to spectral initialization and the compatibility optimizer.

Both default to Euclidean distance, `random_state=None`, and `n_epochs=None`.
A numeric random seed enables repeatability within the same inputs, settings,
platform and dependency versions. Seeded compatibility runs use one worker;
new-pipeline runs remain parallel. `n_jobs=-1` uses EIDORA's existing bounded
projection CPU budget; larger explicit requests are also capped to that budget.
Saved coordinates are never recalculated. Earlier `epochs` and `reproducible`
settings are translated at the parameter boundary; an old compatibility-mode
request selects `umap_05`. Provenance records the library version separately
from the method name, plus effective initialization, optimizer and worker count.

New Projection displays basic controls beside a collapsed Advanced parameters
section. Both sections start from the method schema's defaults. Changing methods
or reopening the dialog restores defaults; collapsing Advanced preserves edits.

| Method | Basic controls and defaults |
| --- | --- |
| UMAP 0.6 / 0.5 | Neighbors: 15; distance: Euclidean; minimum distance: 0.1 |
| PCA | Solver: auto |
| t-SNE | Perplexity: 30; distance: Euclidean |
| t-SNE (openTSNE) | Perplexity: 30; distance: Euclidean |
| MDS | Mode: metric; distance: Euclidean |
| PaCMAP | Neighbors: 10; distance: Euclidean |
| TriMAP | Inliers: 12; distance: Euclidean |

The other supported controls belong to Advanced. PCA defaults to automatic
solver selection and power iterations with whitening off. t-SNE uses automatic
learning rate, PCA initialization, 1,000 iterations and early exaggeration 12.
PaCMAP uses ratios 0.5/2, learning rate 1 and its 100/100/250 phase schedule.
TriMAP uses 12 inliers, 4 outliers, 3 random triplets, 400 iterations, learning
rate 0.1 and weight temperature 0.5. Explicit seeds are optional for all five
library-backed algorithms; None retains library randomness.

These defaults were reviewed against scikit-learn 1.9.0, PaCMAP 0.9.1, TriMAP
1.1.5 and UMAP 0.6.0, and are checked against installed library signatures. References:
[PCA](https://scikit-learn.org/stable/modules/generated/sklearn.decomposition.PCA.html),
[t-SNE](https://scikit-learn.org/stable/modules/generated/sklearn.manifold.TSNE.html),
[PaCMAP](https://github.com/YingfanWang/PaCMAP), and
[TriMAP](https://github.com/eamid/trimap).

EIDORA still requires two output dimensions and bounds neighbor settings for
small datasets. t-SNE retains the library-recommended 50-dimensional PCA
preprocessing option; TriMAP's preprocessing limit is 100, matching its library.
TriMAP retains the PyNNDescent adapter instead of Annoy for native stability.
PaCMAP retains its FAISS backend and accepts legacy reproducibility settings.
These application choices are described in parameter tooltips and effective
run settings. The built-in Random method keeps its deterministic seed 42 for
existing workflows; Random and Custom Coordinates are not offered by this dialog.

MDS uses scikit-learn 1.9's metric/non-metric SMACOF implementation. It explicitly
selects random initialization (the effective 1.9 default), 300 iterations, one
start and automatic stress normalization. Classical MDS initialization is also
available and uses one start. Restarts run sequentially. Before pairwise allocation,
the float64 input and one full distance matrix must fit within the current memory
budget. The larger working-memory estimate (eight dense float64 matrices plus
input/loading costs) produces a warning rather than an arbitrary size limit.
Non-metric MDS treats zero dissimilarities as missing,
including duplicate vectors. Saved execution details include stress, its normalization
and completed iterations. MDS has no arrangement transform because that would
change the distance relationships it optimizes.

The separate `opentsne` entry uses openTSNE 1.0.4 without changing `tsne` or its
saved settings. Defaults match openTSNE: PCA initialization, automatic learning
rate/exaggeration, 250 early plus 500 normal iterations, and one worker. Auto
optimization uses Barnes–Hut below 10,000 items and FFT interpolation otherwise.
Auto neighbor search uses exact search below 1,000 items and PyNNDescent otherwise
to avoid an optional Annoy dependency. Explicit worker requests are capped to the
projection subprocess CPU budget. As with t-SNE, optional 50-dimensional PCA
preprocessing and small-dataset perplexity clamping are recorded. One-dimensional
inputs use random initialization if PCA was requested. Progress is coalesced
across both optimization phases. Only final coordinates are saved; fitted-model
transforms, custom arrays, callbacks and callable metrics are not exposed.

New Projection assesses the current source, filter, method and parameters inline.
Background inspection reads vector headers and item IDs without decoding the
vector payload. Counts use the same canonical item-ID mapping and filter as
execution. Estimates include full-source loading costs where the current reader
loads vectors before filtering, so a filter does not always solve a loading limit.
All methods remain selectable. Invalid configurations and necessary allocations
exceeding 80% of currently available physical memory disable Create Projection;
approximate workspace estimates and expensive runtime settings only warn. The
button always reads Create Projection and changes only its enabled state. The
assessment uses one body font, estimated memory and memory budget on one line, a bold status
line and a bounded explanation area. The remaining 20% is reserved for the app and
OS. Only the most specific runtime explanation is shown: all-pairs processing first,
then expensive iteration/start/neighborhood/sampling settings, input dimensions,
and finally selection size. Default neighborhoods on large selections are explained
by selection size. Memory notices remain separate; solver/backend details are omitted. These hints are not duration predictions or guarantees.
The refresh icon beside Memory budget refreshes source information; settings and memory are reassessed at
submission, and the worker checks again before loading vectors. Source values are
still validated during execution. Obsolete background results are discarded when
the source, filter, project or dialog session changes.

References: [MDS](https://scikit-learn.org/stable/modules/generated/sklearn.manifold.MDS.html)
and [openTSNE](https://opentsne.readthedocs.io/en/stable/api/index.html).

Parameters that
require supervised labels, fitted-model transforms, precomputed neighbor arrays,
density-radius output or non-2D output stay visible but fixed at their defaults,
with explanatory tooltips. Named metrics and named initializers are supported;
Python callables and custom initialization arrays are not accepted.

The dialog uses checkboxes for booleans, numeric controls for numbers and
noneditable dropdowns for fixed option lists. Optional numbers provide
None/Automatic and Custom modes. Compound settings (dictionaries, lists, matrices
and nested options) use free-text JSON fields; nullable settings accept `None`,
`null` or an empty field. Epoch counts may be a single number or a JSON list;
Discovery saves final coordinates. Malformed compound input is reported by the
inline assessment before submission.

Parameter schemas use `label` for display text and `advanced: true` for controls
in the expandable section. Submission always uses each schema's stable key.
`automatic: auto` allows the library's automatic choice alongside a numeric value.
`readonly` fixes unsupported options to their declared defaults; `nullable`
preserves automatic library values. JSON schemas may restrict inputs to objects
or epoch counts. Catalog discovery must remain free from UMAP/Numba imports.
