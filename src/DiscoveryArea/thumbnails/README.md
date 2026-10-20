# Thumbnail storage and decoding

`repository.py` owns variants and their indexes. `resolver.py` maps canonical
metadata IDs to sources; `service.py` performs asynchronous decoding and
`cache.py` bounds decoded memory.

Datasets creates new variants through this repository. Publish staged files and
their index together; clean failed/cancelled staging and preserve partial-success
counts. New variants contain individual images; the native renderer packs its atlas.

Keep source aspect ratios. Clear stale requests when variants or sizes change,
and distinguish reusable decoded cache entries from current drawable membership.
Callbacks from decode workers must be marshalled before touching Qt widgets.
