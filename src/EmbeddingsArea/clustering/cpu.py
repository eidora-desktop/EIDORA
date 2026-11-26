"""Translate a CPU allocation percentage into a logical-thread budget."""


def available_cpu_threads():
    # joblib already ships with sklearn and respects affinity and CPU quotas.
    from joblib import cpu_count

    return max(1, cpu_count())


def clustering_cpu_budget(percentage=75, *, available=None):
    available = available_cpu_threads() if available is None else max(1, available)
    return max(1, min(available, available * percentage // 100))
