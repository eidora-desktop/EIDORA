"""Resolve a reusable slicer recipe against its ordered metadata values."""


def sorted_slicer_values(values) -> list[str]:
    values = sorted(set(map(str, values)), key=lambda value: (value.lower(), value))
    try:
        return sorted(values, key=float)
    except ValueError:
        return values


def slicer_selection(payload: dict, values) -> dict:
    state = payload.get("slicer", payload)
    column = str(state.get("column") or "")
    if not state.get("enabled", bool(column)) or not column:
        return {}
    if isinstance(state.get("values"), list):
        return {"column": column, "values": list(state["values"])}
    ordered = sorted_slicer_values(values)
    if not ordered:
        return {"column": column, "values": []}
    spread = max(1, int(state.get("spread", 1)))
    position = max(0, int(state.get("position", 0)))
    try:
        numbers = [float(value) for value in ordered]
        numeric = all(value.is_integer() for value in numbers)
    except ValueError:
        numeric = False
    minimum = int(numbers[0]) if numeric else 0
    maximum = int(numbers[-1]) if numeric else len(ordered) - 1
    start = min(maximum, max(minimum, int(state.get("start", minimum))))
    limit = max(0, (maximum if numeric else len(ordered)) - start - spread)
    position = min(position, limit)
    step = max(1, int(state.get("step", 1)))
    if position < limit:
        position = min(((position + step // 2) // step) * step, limit)
    start += position
    selected = ([value for value, number in zip(ordered, numbers) if start <= number <= start + spread]
                if numeric else ordered[start:start + spread])
    return {"column": column, "values": selected}
