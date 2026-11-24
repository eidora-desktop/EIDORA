"""Validate image-preview encoding options shared by images and video stills."""


def image_parameters(parameters):
    quality = int(parameters.get("image_quality", 85))
    seed = int(parameters.get("random_seed", 0))
    if not 1 <= quality <= 100:
        raise ValueError("Image quality must be between 1 and 100.")
    if not 0 <= seed <= 2147483647:
        raise ValueError("Random seed must be between 0 and 2147483647.")
    return {"image_quality": quality, "random_seed": seed}
