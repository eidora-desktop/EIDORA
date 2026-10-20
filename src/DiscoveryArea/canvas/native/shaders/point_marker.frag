#version 440

layout(location = 0) in vec2 vLocalCoord;
layout(location = 1) in vec4 vFillColor;
layout(location = 2) in vec4 vEdgeColor;
layout(location = 3) in float vInnerRatio;
layout(location = 4) in float vOuterRatio;

layout(location = 0) out vec4 fragColor;

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
    vec2 spriteCoord = (gl_PointCoord * 2.0) - 1.0;
    vec2 pointCoord = mix(vLocalCoord, spriteCoord, usePointCoord) / max(vOuterRatio, 1e-4);
    float radialDistance = length(pointCoord);
    float outerDistance = radialDistance - 1.0;
    float innerDistance = radialDistance - max(vInnerRatio, 1e-4);
    float aa = max(fwidth(outerDistance), 0.002);
    float outerCoverage = 1.0 - smoothstep(-aa, aa, outerDistance);
    float innerCoverage = 1.0 - smoothstep(-aa, aa, innerDistance);
    if (outerCoverage <= 0.0) {
        discard;
    }
    vec4 color = mix(vEdgeColor, vFillColor, innerCoverage);
    color.a *= outerCoverage * opacity;
    if (color.a <= 0.0) {
        discard;
    }
    color.rgb *= color.a;
    fragColor = color;
}
