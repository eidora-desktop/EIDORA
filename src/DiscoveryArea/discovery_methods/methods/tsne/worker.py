from __future__ import annotations

from pathlib import Path
import sys

import numpy as np


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: worker <input.npz> <output.npz>", file=sys.stderr)
        return 2
    input_path = Path(args[0])
    output_path = Path(args[1])
    try:
        with np.load(input_path, allow_pickle=False) as payload:
            vectors = np.asarray(payload["vectors"], dtype=np.float32)
            perplexity = float(payload["perplexity"][0])
            learning_rate = str(payload["learning_rate"][0])
            init = str(payload["init"][0])
            random_state = int(payload["random_state"][0])
        from sklearn.manifold import TSNE

        reducer = TSNE(
            n_components=2,
            perplexity=perplexity,
            learning_rate=learning_rate,
            init=init,
            random_state=random_state,
        )
        coordinates = np.asarray(reducer.fit_transform(vectors), dtype=np.float32)[:, :2]
        np.savez(output_path, coordinates=coordinates)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
