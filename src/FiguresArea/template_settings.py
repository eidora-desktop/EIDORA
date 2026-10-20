from __future__ import annotations

from FiguresArea.domain import FigurePlotSettings


def default_plot_settings(figure_kind: str) -> FigurePlotSettings:
    """Return the visual profile for a newly created figure template."""

    kind = str(figure_kind).lower()
    if kind == "journal":
        return FigurePlotSettings(
            width_mm=85.0,
            height_mm=70.0,
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
