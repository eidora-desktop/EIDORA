#version 440

layout(location = 0) in vec2 worldCenter;
layout(location = 2) in vec4 fillColor;
layout(location = 3) in vec4 edgeColor;
layout(location = 4) in float pointRadius;
layout(location = 5) in float edgeWidth;

layout(location = 0) out vec2 vLocalCoord;
layout(location = 1) out vec4 vFillColor;
layout(location = 2) out vec4 vEdgeColor;
layout(location = 3) out float vInnerRatio;
layout(location = 4) out float vOuterRatio;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float opacity;
    vec4 viewport;
    vec2 itemSize;
    float antialiasPx;
    float devicePixelRatio;
    float usePointCoord;
};

void main()
{
    vec2 span = max(viewport.yw - viewport.xz, vec2(1e-9));
    vec2 normalized = (worldCenter - viewport.xz) / span;
    vec2 centerPx = vec2(normalized.x * itemSize.x, (1.0 - normalized.y) * itemSize.y);
    float outerRadius = max(0.5, pointRadius + edgeWidth);
    gl_PointSize = 2.0 * (outerRadius + antialiasPx) * devicePixelRatio;
    vLocalCoord = vec2(0.0);
    vFillColor = fillColor;
    vEdgeColor = edgeColor;
    vInnerRatio = clamp(pointRadius / outerRadius, 0.0, 1.0);
    vOuterRatio = outerRadius / (outerRadius + antialiasPx);
    gl_Position = qt_Matrix * vec4(centerPx, 0.0, 1.0);
}
