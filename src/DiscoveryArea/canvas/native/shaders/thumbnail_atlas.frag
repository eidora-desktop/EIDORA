#version 440

layout(location = 0) in vec2 vTexCoord;
layout(location = 1) in vec4 vColor;
layout(location = 2) in vec2 vLocalCoord;
layout(location = 3) in vec2 vBorderFraction;
layout(location = 4) in vec4 vBorderColor;

layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float opacity;
    int colorMode;
    float tintStrength;
    float bodyOpacity;
    vec4 viewport;
    vec2 canvasSize;
    int displayEnabled;
    float padding1;
};

layout(binding = 1) uniform sampler2D atlasTexture;

void main()
{
    vec4 tex = texture(atlasTexture, vTexCoord);
    vec4 outputColor = tex;

    if (colorMode == 2 || colorMode == 3) {
        vec3 screenTint = 1.0 - ((1.0 - tex.rgb) * (1.0 - vColor.rgb));
        outputColor.rgb = mix(tex.rgb, screenTint, tintStrength * vColor.a);
    }

    if (colorMode == 1 || colorMode == 3) {
        float edgeX = min(vLocalCoord.x, 1.0 - vLocalCoord.x);
        float edgeY = min(vLocalCoord.y, 1.0 - vLocalCoord.y);
        if (edgeX <= vBorderFraction.x || edgeY <= vBorderFraction.y) {
            outputColor.rgb = vBorderColor.rgb;
            outputColor.a = vBorderColor.a;
            outputColor.a *= opacity;
        } else {
            outputColor.a *= opacity * bodyOpacity;
        }
    } else {
        outputColor.a *= opacity * bodyOpacity;
    }
    fragColor = outputColor;
}
