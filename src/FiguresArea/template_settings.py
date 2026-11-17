from __future__ import annotations

from dataclasses import replace

from FiguresArea.domain import FigurePlotSettings


def saved_plot_defaults(figure_kind: str) -> FigurePlotSettings:
    """Frozen fallback values for existing documents and older partial presets."""

    kind = str(figure_kind).lower()
    if kind == "journal":
        return FigurePlotSettings(
            width_mm=85.0,
            height_mm=70.0,
            plot_width_mm=85.0,
            plot_height_mm=70.0,
            margin_mm=2.0,
            subplot_margin_mm=3.0,
            export_dpi=300,
            axis_box_line_width=0.0,
            grid_mode="off",
            grid_line_width=0.4,
            tick_label_size=8,
            tick_label_margin_pt=2.0,
            main_title_size=11,
            subtitle_size=9,
            legend_font_size=8,
            legend_marker_size=6.0,
            legend_frame=False,
            pin_line_width=0.8,
            main_caption_size=8,
            main_caption_line_spacing=1.15,
            subplot_caption_size=7,
            subplot_caption_line_spacing=1.15,
        )
    if kind == "presentation":
        return FigurePlotSettings(
            width_mm=160.0,
            height_mm=90.0,
            plot_width_mm=160.0,
            plot_height_mm=90.0,
            margin_mm=3.0,
            subplot_margin_mm=5.0,
            export_dpi=144,
            axis_box_line_width=0.0,
            grid_mode="off",
            grid_line_width=0.7,
            tick_label_size=13,
            tick_label_margin_pt=3.0,
            point_size_factor_percent=30.0,
            main_title_size=20,
            subtitle_size=16,
            legend_font_size=13,
            legend_marker_size=10.0,
            legend_frame=False,
            pin_line_width=1.3,
            main_caption_size=12,
            main_caption_line_spacing=1.2,
            subplot_caption_size=11,
            subplot_caption_line_spacing=1.2,
        )
    return FigurePlotSettings(grid_mode="off")


# All values below are output pixels. The conversion keeps the established
# renderer/storage contract separate from the new-document presets.
_STARTING_PROFILES = {
    'journal': dict(width=1600, height=1200, margin=40, spacing=32,
                    title=48, subtitle=32, labels=24, caption=22, grid=1),
    'presentation': dict(width=1920, height=1080, margin=48, spacing=40,
                         title=56, subtitle=36, labels=28, caption=24, grid=1.5),
    'animation': dict(width=800, height=600, margin=24, spacing=20,
                      title=28, subtitle=22, labels=18, caption=16, grid=1),
}


def default_plot_settings(figure_kind: str) -> FigurePlotSettings:
    """Return a pixel-based starting profile, only for newly created figures."""
    from FiguresArea.measurements import pixels_per_unit
    kind = str(figure_kind).lower()
    settings = saved_plot_defaults(kind)
    profile = _STARTING_PROFILES.get(kind)
    if profile is None:
        return settings
    pixels = {
        'width_mm': profile['width'], 'height_mm': profile['height'],
        'margin_mm': profile['margin'], 'subplot_margin_mm': profile['spacing'],
        'main_title_size': profile['title'], 'subtitle_size': profile['subtitle'],
        'legend_font_size': profile['labels'], 'tick_label_size': profile['labels'],
        'main_caption_size': profile['caption'], 'subplot_caption_size': profile['caption'],
        'grid_line_width': profile['grid'],
    }
    converted = {key: value / pixels_per_unit(settings, key) for key, value in pixels.items()}
    # The initial plot occupies the page inside its margins at scale 1. Later
    # grid/decorations edits retain the usual proportional scaling behavior.
    return replace(settings, **converted,
        plot_width_mm=converted['width_mm'] - 2 * converted['margin_mm'],
        plot_height_mm=converted['height_mm'] - 2 * converted['margin_mm'],
        point_size_factor_percent=0.0, legend_frame=False)
