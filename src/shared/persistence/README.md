# Persistence helpers

`json_store.py` reads JSON and writes through `atomic_write.py`. Atomic text writes
stage beside the destination, replace it only after writing, and clean staging
on failure. This is file replacement, not a transaction spanning several files.

`sqlite.py` opens connections with foreign keys enabled. The caller must close
each connection: a SQLite `with connection:` block only commits or rolls back.
Batch large ID queries below portable bind-variable limits.

Keep schemas and publication decisions in the owning feature repository.
