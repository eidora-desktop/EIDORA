#version 440

layout(location = 0) in vec2 worldPosition;
layout(location = 1) in vec2 texCoord;
layout(location = 2) in vec4 color;
layout(location = 3) in vec2 pixelOffset;
layout(location = 4) in vec2 localCoord;
layout(location = 5) in vec2 vertexBorderFraction;
layout(location = 6) in vec4 borderColor;

layout(location = 0) out vec2 vTexCoord;
layout(location = 1) out vec4 vColor;
layout(location = 2) out vec2 vLocalCoord;
layout(location = 3) out vec2 vBorderFraction;
layout(location = 4) out vec4 vBorderColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float opacity;
    int colorMode;
    float tintStrength;
    float padding0;
    vec4 viewport;
    vec2 canvasSize;
    int displayEnabled;
    float padding1;
};

void main()
{
    vTexCoord = texCoord;
    vColor = color;
    vLocalCoord = localCoord;
    vBorderFraction = vertexBorderFraction;
    vBorderColor = borderColor;
    float spanX = max(0.000000001, viewport.y - viewport.x);
    float spanY = max(0.000000001, viewport.w - viewport.z);
    vec2 itemPosition = vec2(
        ((worldPosition.x - viewport.x) / spanX) * canvasSize.x,
        canvasSize.y - (((worldPosition.y - viewport.z) / spanY) * canvasSize.y)
    );
    if (displayEnabled == 0) {
        itemPosition = vec2(-100000.0);
    }
    gl_Position = qt_Matrix * vec4(itemPosition + pixelOffset, 0.0, 1.0);
}
