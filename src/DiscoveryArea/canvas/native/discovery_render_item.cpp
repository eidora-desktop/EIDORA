#include "discovery_render_item.h"

#include <QColor>
#include <QByteArray>
#include <QSGGeometry>
#include <QSGGeometryNode>
#include <QSGMaterial>
#include <QSGMaterialShader>
#include <QSGRendererInterface>
#include <QSGSimpleTextureNode>
#include <QSGOpacityNode>
#include <QSGTexture>
#include <QSGTransformNode>
#include <QMatrix4x4>
#include <QImageReader>
#include <QFileInfo>
#include <QMetaObject>
#include <QPainter>
#include <QPointer>
#include <QFutureWatcher>
#include <QElapsedTimer>
#include <QtGui/rhi/qrhi.h>
#include <QtConcurrent>
#include <QtGlobal>
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <limits>
#include <cstring>

namespace {

constexpr float kHoverPointScaleFactor = 4.0f;
constexpr double kHoverThumbnailScaleFactor = 1.3;

struct OverviewRasterResult {
    QImage image;
    QRectF worldRect;
};

struct ParsedPointBuffers {
    QVector<QPointF> points;
    QVector<int> metadataIds;
    QHash<int, int> pointIndexByMetadataId;
    QVector<float> visibility;
    QVector<QColor> colors;
    QVector<QColor> edgeColors;
    QVector<float> pointSizes;
    QVector<float> edgeWidths;
    bool hasVisibility = false;
    bool hasColors = false;
    bool hasEdgeColors = false;
    bool hasPointSizes = false;
    bool hasEdgeWidths = false;
    int pointCount = 0;
    double pointSize = 1.0;
    double preparationMs = 0.0;
};

struct ParsedPointStyleBuffers {
    QVector<QColor> colors;
    QVector<QColor> edgeColors;
    QVector<float> pointSizes;
    QVector<float> edgeWidths;
    bool hasColors = false;
    bool hasEdgeColors = false;
    bool hasPointSizes = false;
    bool hasEdgeWidths = false;
    double pointSize = 1.0;
};

struct PointMarkerVertex {
    float centerX = 0.0f;
    float centerY = 0.0f;
    quint8 fillR = 255;
    quint8 fillG = 255;
    quint8 fillB = 255;
    quint8 fillA = 255;
    quint8 edgeR = 255;
    quint8 edgeG = 255;
    quint8 edgeB = 255;
    quint8 edgeA = 255;
    float radius = 1.0f;
    float edgeWidth = 0.0f;
};

static_assert(sizeof(PointMarkerVertex) == 24, "Point marker vertex layout must stay compact");

struct QuadMarkerVertex {
    float centerX = 0.0f;
    float centerY = 0.0f;
    float localX = 0.0f;
    float localY = 0.0f;
    quint8 fillR = 255;
    quint8 fillG = 255;
    quint8 fillB = 255;
    quint8 fillA = 255;
    quint8 edgeR = 255;
    quint8 edgeG = 255;
    quint8 edgeB = 255;
    quint8 edgeA = 255;
    float radius = 1.0f;
    float edgeWidth = 0.0f;
};

static_assert(sizeof(QuadMarkerVertex) == 32, "Portable marker vertex layout must stay compact");

const QSGGeometry::AttributeSet& pointMarkerAttributes() {
    static const QSGGeometry::Attribute attributes[] = {
        QSGGeometry::Attribute::create(0, 2, QSGGeometry::FloatType, true),
        QSGGeometry::Attribute::createWithAttributeType(
            2, 4, QSGGeometry::UnsignedByteType, QSGGeometry::ColorAttribute
        ),
        QSGGeometry::Attribute::createWithAttributeType(
            3, 4, QSGGeometry::UnsignedByteType, QSGGeometry::ColorAttribute
        ),
        QSGGeometry::Attribute::create(4, 1, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(5, 1, QSGGeometry::FloatType),
    };
    static const QSGGeometry::AttributeSet attributeSet = {
        5,
        static_cast<int>(sizeof(PointMarkerVertex)),
        attributes,
    };
    return attributeSet;
}

const QSGGeometry::AttributeSet& quadMarkerAttributes() {
    static const QSGGeometry::Attribute attributes[] = {
        QSGGeometry::Attribute::create(0, 2, QSGGeometry::FloatType, true),
        QSGGeometry::Attribute::create(1, 2, QSGGeometry::FloatType),
        QSGGeometry::Attribute::createWithAttributeType(
            2, 4, QSGGeometry::UnsignedByteType, QSGGeometry::ColorAttribute
        ),
        QSGGeometry::Attribute::createWithAttributeType(
            3, 4, QSGGeometry::UnsignedByteType, QSGGeometry::ColorAttribute
        ),
        QSGGeometry::Attribute::create(4, 1, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(5, 1, QSGGeometry::FloatType),
    };
    static const QSGGeometry::AttributeSet attributeSet = {
        6,
        static_cast<int>(sizeof(QuadMarkerVertex)),
        attributes,
    };
    return attributeSet;
}

class PointMarkerMaterial;

class PointMarkerMaterialShader final : public QSGMaterialShader {
public:
    explicit PointMarkerMaterialShader(bool portableQuads) {
        setShaderFileName(
            VertexStage,
            portableQuads
                ? QStringLiteral(":/DiscoveryRenderer/shaders/point_marker_quad.vert.qsb")
                : QStringLiteral(":/DiscoveryRenderer/shaders/point_marker.vert.qsb")
        );
        setShaderFileName(FragmentStage, QStringLiteral(":/DiscoveryRenderer/shaders/point_marker.frag.qsb"));
    }

    bool updateUniformData(
        RenderState& state,
        QSGMaterial* newMaterial,
        QSGMaterial* oldMaterial
    ) override;
};

class PointMarkerMaterial final : public QSGMaterial {
public:
    explicit PointMarkerMaterial(bool portableQuads) : portableQuads(portableQuads) {
        setFlag(QSGMaterial::Blending, true);
        setFlag(QSGMaterial::NoBatching, true);
    }

    QSGMaterialType* type() const override {
        static QSGMaterialType pointSpriteMaterialType;
        static QSGMaterialType portableQuadMaterialType;
        return portableQuads ? &portableQuadMaterialType : &pointSpriteMaterialType;
    }

    QSGMaterialShader* createShader(QSGRendererInterface::RenderMode renderMode) const override {
        Q_UNUSED(renderMode);
        return new PointMarkerMaterialShader(portableQuads);
    }

    int compare(const QSGMaterial* other) const override {
        Q_UNUSED(other);
        return 0;
    }

    float viewportXMin = 0.0f;
    float viewportXMax = 1.0f;
    float viewportYMin = 0.0f;
    float viewportYMax = 1.0f;
    float itemWidth = 1.0f;
    float itemHeight = 1.0f;
    float antialiasPx = 1.25f;
    float devicePixelRatio = 1.0f;
    bool portableQuads = false;
};

bool PointMarkerMaterialShader::updateUniformData(
    RenderState& state,
    QSGMaterial* newMaterial,
    QSGMaterial* oldMaterial
) {
    Q_UNUSED(oldMaterial);
    auto* material = static_cast<PointMarkerMaterial*>(newMaterial);
    QByteArray* buffer = state.uniformData();
    constexpr int requiredSize = 128;
    if (buffer->size() < requiredSize) {
        buffer->resize(requiredSize);
    }
    const QMatrix4x4 matrix = state.combinedMatrix();
    std::memcpy(buffer->data(), matrix.constData(), 64);
    const float opacity = state.opacity();
    std::memcpy(buffer->data() + 64, &opacity, sizeof(float));
    const float viewport[] = {
        material->viewportXMin,
        material->viewportXMax,
        material->viewportYMin,
        material->viewportYMax,
    };
    std::memcpy(buffer->data() + 80, viewport, sizeof(viewport));
    const float itemSize[] = {material->itemWidth, material->itemHeight};
    std::memcpy(buffer->data() + 96, itemSize, sizeof(itemSize));
    std::memcpy(buffer->data() + 104, &material->antialiasPx, sizeof(float));
    std::memcpy(buffer->data() + 108, &material->devicePixelRatio, sizeof(float));
    const float usePointCoord = material->portableQuads ? 0.0f : 1.0f;
    std::memcpy(buffer->data() + 112, &usePointCoord, sizeof(float));
    return true;
}

OverviewRasterResult buildOverviewRaster(
    const QVector<QPointF>& points,
    const QVector<float>& visibility,
    bool hasVisibility,
    const QVector<QColor>& colors,
    bool hasColors
) {
    OverviewRasterResult result;
    if (points.isEmpty()) {
        return result;
    }
    double xMin = std::numeric_limits<double>::max();
    double xMax = std::numeric_limits<double>::lowest();
    double yMin = std::numeric_limits<double>::max();
    double yMax = std::numeric_limits<double>::lowest();
    for (const QPointF& point : points) {
        if (!std::isfinite(point.x()) || !std::isfinite(point.y())) {
            continue;
        }
        xMin = qMin(xMin, point.x());
        xMax = qMax(xMax, point.x());
        yMin = qMin(yMin, point.y());
        yMax = qMax(yMax, point.y());
    }
    const double spanX = xMax - xMin;
    const double spanY = yMax - yMin;
    if (!std::isfinite(spanX) || !std::isfinite(spanY) || spanX <= 1e-12 || spanY <= 1e-12) {
        return result;
    }
    constexpr int maxDimension = 1536;
    const double aspect = spanX / spanY;
    const int width = aspect >= 1.0 ? maxDimension : qMax(64, static_cast<int>(std::round(maxDimension * aspect)));
    const int height = aspect >= 1.0 ? qMax(64, static_cast<int>(std::round(maxDimension / aspect))) : maxDimension;
    const qsizetype pixelCount = static_cast<qsizetype>(width) * static_cast<qsizetype>(height);
    QVector<float> sumR(pixelCount, 0.0f);
    QVector<float> sumG(pixelCount, 0.0f);
    QVector<float> sumB(pixelCount, 0.0f);
    QVector<float> sumA(pixelCount, 0.0f);
    for (int index = 0; index < points.size(); ++index) {
        float visibilityAlpha = 1.0f;
        if (hasVisibility && index < visibility.size()) {
            visibilityAlpha = visibility.at(index);
            if (visibilityAlpha <= 0.0f) {
                continue;
            }
        }
        const QPointF& point = points.at(index);
        if (!std::isfinite(point.x()) || !std::isfinite(point.y())) {
            continue;
        }
        const int px = qBound(0, static_cast<int>(((point.x() - xMin) / spanX) * (width - 1)), width - 1);
        const int py = qBound(0, static_cast<int>(((yMax - point.y()) / spanY) * (height - 1)), height - 1);
        const qsizetype pixelIndex = static_cast<qsizetype>(py) * width + px;
        QColor color = hasColors && index < colors.size() ? colors.at(index) : QColor::fromRgbF(0.12, 0.12, 0.12, 1.0);
        const float alpha = qBound(0.0f, static_cast<float>(color.alphaF()) * visibilityAlpha, 1.0f);
        sumR[pixelIndex] += static_cast<float>(color.redF()) * alpha;
        sumG[pixelIndex] += static_cast<float>(color.greenF()) * alpha;
        sumB[pixelIndex] += static_cast<float>(color.blueF()) * alpha;
        sumA[pixelIndex] += alpha;
    }
    QImage image(width, height, QImage::Format_RGBA8888_Premultiplied);
    image.fill(Qt::transparent);
    for (int y = 0; y < height; ++y) {
        auto* rgba = image.scanLine(y);
        for (int x = 0; x < width; ++x) {
            const qsizetype index = static_cast<qsizetype>(y) * width + x;
            const float accumulatedAlpha = sumA.at(index);
            if (accumulatedAlpha <= 0.0f) {
                continue;
            }
            // The raster represents the complete dataset, but is only a quiet
            // continuity cue beneath the point-cloud LOD. Keeping it faint
            // prevents rectangular density cells from becoming the visual.
            constexpr float overviewOpacity = 0.10f;
            const float alpha = (1.0f - std::exp(-accumulatedAlpha * 0.72f)) * overviewOpacity;
            const float inverse = 1.0f / accumulatedAlpha;
            rgba[x * 4] = static_cast<uchar>(qBound(0.0f, sumR.at(index) * inverse * alpha, 1.0f) * 255.0f);
            rgba[x * 4 + 1] = static_cast<uchar>(qBound(0.0f, sumG.at(index) * inverse * alpha, 1.0f) * 255.0f);
            rgba[x * 4 + 2] = static_cast<uchar>(qBound(0.0f, sumB.at(index) * inverse * alpha, 1.0f) * 255.0f);
            rgba[x * 4 + 3] = static_cast<uchar>(alpha * 255.0f);
        }
    }
    result.image = std::move(image);
    result.worldRect = QRectF(xMin, yMin, spanX, spanY);
    return result;
}

class DiscoveryRootNode final : public QSGNode {
public:
    explicit DiscoveryRootNode(bool portableQuads) : portableQuads(portableQuads) {
        pointTransform = new QSGTransformNode();
        pointOpacity = new QSGOpacityNode();
        pointNode = new QSGGeometryNode();
        auto* geometry = new QSGGeometry(
            portableQuads ? quadMarkerAttributes() : pointMarkerAttributes(),
            0,
            0,
            portableQuads ? QSGGeometry::UnsignedIntType : QSGGeometry::UnsignedShortType
        );
        geometry->setDrawingMode(portableQuads ? QSGGeometry::DrawTriangles : QSGGeometry::DrawPoints);
        pointNode->setGeometry(geometry);
        pointNode->setFlag(QSGNode::OwnsGeometry, true);

        auto* material = new PointMarkerMaterial(portableQuads);
        pointNode->setMaterial(material);
        pointNode->setFlag(QSGNode::OwnsMaterial, true);

        hoveredPointNode = new QSGGeometryNode();
        auto* hoveredGeometry = new QSGGeometry(
            portableQuads ? quadMarkerAttributes() : pointMarkerAttributes(),
            0,
            0,
            portableQuads ? QSGGeometry::UnsignedIntType : QSGGeometry::UnsignedShortType
        );
        hoveredGeometry->setDrawingMode(
            portableQuads ? QSGGeometry::DrawTriangles : QSGGeometry::DrawPoints
        );
        hoveredPointNode->setGeometry(hoveredGeometry);
        hoveredPointNode->setFlag(QSGNode::OwnsGeometry, true);
        auto* hoveredMaterial = new PointMarkerMaterial(portableQuads);
        hoveredPointNode->setMaterial(hoveredMaterial);
        hoveredPointNode->setFlag(QSGNode::OwnsMaterial, true);

        pointTransform->appendChildNode(pointNode);
        pointOpacity->appendChildNode(pointTransform);
        appendChildNode(pointOpacity);
        appendChildNode(hoveredPointNode);
    }

    QSGTransformNode* pointTransform = nullptr;
    QSGGeometryNode* pointNode = nullptr;
    QSGGeometryNode* hoveredPointNode = nullptr;
    QSGSimpleTextureNode* overviewNode = nullptr;
    QSGOpacityNode* pointOpacity = nullptr;
    bool portableQuads = false;
    QVector<QSGGeometryNode*> thumbnailAtlasNodes;
    QSGGeometryNode* hoveredThumbnailAtlasNode = nullptr;
};

void releaseHoveredThumbnailAtlasNode(DiscoveryRootNode* root) {
    if (root == nullptr || root->hoveredThumbnailAtlasNode == nullptr) {
        return;
    }
    if (root->hoveredThumbnailAtlasNode->parent() != nullptr) {
        root->hoveredThumbnailAtlasNode->parent()->removeChildNode(root->hoveredThumbnailAtlasNode);
    }
    delete root->hoveredThumbnailAtlasNode;
    root->hoveredThumbnailAtlasNode = nullptr;
}

void releaseThumbnailAtlasNodes(DiscoveryRootNode* root) {
    if (root == nullptr) {
        return;
    }
    releaseHoveredThumbnailAtlasNode(root);
    for (QSGGeometryNode* node : root->thumbnailAtlasNodes) {
        if (node == nullptr) {
            continue;
        }
        if (node->parent() != nullptr) {
            node->parent()->removeChildNode(node);
        }
        delete node;
    }
    root->thumbnailAtlasNodes.clear();
}

struct ThumbnailAtlasVertex {
    float worldX = 0.0f;
    float worldY = 0.0f;
    float u = 0.0f;
    float v = 0.0f;
    float r = 1.0f;
    float g = 1.0f;
    float b = 1.0f;
    float a = 1.0f;
    float offsetX = 0.0f;
    float offsetY = 0.0f;
    float localX = 0.0f;
    float localY = 0.0f;
    float borderX = 0.0f;
    float borderY = 0.0f;
    float borderR = 1.0f;
    float borderG = 1.0f;
    float borderB = 1.0f;
    float borderA = 1.0f;
};

const QSGGeometry::AttributeSet& thumbnailAtlasAttributes() {
    static const QSGGeometry::Attribute attributes[] = {
        QSGGeometry::Attribute::create(0, 2, QSGGeometry::FloatType, true),
        QSGGeometry::Attribute::createWithAttributeType(1, 2, QSGGeometry::FloatType, QSGGeometry::TexCoordAttribute),
        QSGGeometry::Attribute::create(2, 4, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(3, 2, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(4, 2, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(5, 2, QSGGeometry::FloatType),
        QSGGeometry::Attribute::create(6, 4, QSGGeometry::FloatType),
    };
    static const QSGGeometry::AttributeSet attributeSet = {
        7,
        static_cast<int>(sizeof(ThumbnailAtlasVertex)),
        attributes,
    };
    return attributeSet;
}

class ThumbnailAtlasMaterialShader final : public QSGMaterialShader {
public:
    ThumbnailAtlasMaterialShader() {
        setShaderFileName(VertexStage, QStringLiteral(":/DiscoveryRenderer/shaders/thumbnail_atlas.vert.qsb"));
        setShaderFileName(FragmentStage, QStringLiteral(":/DiscoveryRenderer/shaders/thumbnail_atlas.frag.qsb"));
    }

    bool updateUniformData(
        RenderState& state,
        QSGMaterial* newMaterial,
        QSGMaterial* oldMaterial
    ) override;

    void updateSampledImage(
        RenderState& state,
        int binding,
        QSGTexture** texture,
        QSGMaterial* newMaterial,
        QSGMaterial* oldMaterial
    ) override;
};

class ThumbnailAtlasMaterial final : public QSGMaterial {
public:
    explicit ThumbnailAtlasMaterial(QSGTexture* atlasTexture) : texture(atlasTexture) {
        setFlag(QSGMaterial::Blending, true);
        setFlag(QSGMaterial::NoBatching, true);
    }

    ~ThumbnailAtlasMaterial() override {
        delete texture;
    }

    QSGMaterialType* type() const override {
        static QSGMaterialType materialType;
        return &materialType;
    }

    QSGMaterialShader* createShader(QSGRendererInterface::RenderMode renderMode) const override {
        Q_UNUSED(renderMode);
        return new ThumbnailAtlasMaterialShader();
    }

    int compare(const QSGMaterial* other) const override {
        const auto* rhs = static_cast<const ThumbnailAtlasMaterial*>(other);
        if (texture < rhs->texture) {
            return -1;
        }
        if (texture > rhs->texture) {
            return 1;
        }
        if (colorMode != rhs->colorMode) {
            return colorMode < rhs->colorMode ? -1 : 1;
        }
        if (!qFuzzyCompare(bodyOpacity, rhs->bodyOpacity)) {
            return bodyOpacity < rhs->bodyOpacity ? -1 : 1;
        }
        return 0;
    }

    QSGTexture* texture = nullptr;
    int colorMode = 0;
    float tintStrength = 1.0f;
    float bodyOpacity = 1.0f;
    float viewportXMin = 0.0f;
    float viewportXMax = 1.0f;
    float viewportYMin = 0.0f;
    float viewportYMax = 1.0f;
    float canvasWidth = 1.0f;
    float canvasHeight = 1.0f;
    int displayEnabled = 1;
};

class ThumbnailAtlasTexture final : public QSGTexture {
public:
    explicit ThumbnailAtlasTexture(const QImage& image)
        : m_size(image.size())
        , m_initialImage(image)
    {
    }

    ~ThumbnailAtlasTexture() override {
        delete m_texture;
    }

    qint64 comparisonKey() const override {
        return static_cast<qint64>(reinterpret_cast<quintptr>(this));
    }

    QRhiTexture* rhiTexture() const override {
        return m_texture;
    }

    QSize textureSize() const override {
        return m_size;
    }

    bool hasAlphaChannel() const override {
        return true;
    }

    bool hasMipmaps() const override {
        return false;
    }

    void queueRegion(const QRect& rect, const QImage& image) {
        if (rect.isEmpty() || image.isNull()) {
            return;
        }
        PendingRegion pending;
        pending.rect = rect;
        pending.image = image;
        m_pendingRegions.append(pending);
    }

    void commitTextureOperations(
        QRhi* rhi,
        QRhiResourceUpdateBatch* resourceUpdates
    ) override {
        if (rhi == nullptr || resourceUpdates == nullptr) {
            return;
        }
        if (m_texture == nullptr) {
            m_texture = rhi->newTexture(QRhiTexture::RGBA8, m_size);
            if (m_texture == nullptr || !m_texture->create()) {
                delete m_texture;
                m_texture = nullptr;
                return;
            }
            resourceUpdates->uploadTexture(m_texture, m_initialImage);
            m_initialImage = QImage();
            m_pendingRegions.clear();
            return;
        }
        if (m_pendingRegions.isEmpty()) {
            return;
        }
        QVector<QRhiTextureUploadEntry> entries;
        entries.reserve(m_pendingRegions.size());
        for (const PendingRegion& pending : m_pendingRegions) {
            QRhiTextureSubresourceUploadDescription description(pending.image);
            description.setDestinationTopLeft(pending.rect.topLeft());
            entries.append(QRhiTextureUploadEntry(0, 0, description));
        }
        QRhiTextureUploadDescription upload;
        upload.setEntries(entries.cbegin(), entries.cend());
        resourceUpdates->uploadTexture(m_texture, upload);
        m_pendingRegions.clear();
    }

private:
    struct PendingRegion {
        QRect rect;
        QImage image;
    };

    QSize m_size;
    QImage m_initialImage;
    QVector<PendingRegion> m_pendingRegions;
    QRhiTexture* m_texture = nullptr;
};

bool ThumbnailAtlasMaterialShader::updateUniformData(
    RenderState& state,
    QSGMaterial* newMaterial,
    QSGMaterial* oldMaterial
) {
    Q_UNUSED(oldMaterial);
    auto* material = static_cast<ThumbnailAtlasMaterial*>(newMaterial);
    QByteArray* buffer = state.uniformData();
    const int requiredSize = 112;
    if (buffer->size() < requiredSize) {
        buffer->resize(requiredSize);
    }
    const QMatrix4x4 matrix = state.combinedMatrix();
    std::memcpy(buffer->data(), matrix.constData(), 64);
    const float opacity = state.opacity();
    const int colorMode = material != nullptr ? material->colorMode : 0;
    const float tintStrength = material != nullptr ? material->tintStrength : 1.0f;
    const float bodyOpacity = material != nullptr ? material->bodyOpacity : 1.0f;
    const float padding = 0.0f;
    std::memcpy(buffer->data() + 64, &opacity, sizeof(float));
    std::memcpy(buffer->data() + 68, &colorMode, sizeof(int));
    std::memcpy(buffer->data() + 72, &tintStrength, sizeof(float));
    std::memcpy(buffer->data() + 76, &bodyOpacity, sizeof(float));
    const float viewport[4] = {
        material != nullptr ? material->viewportXMin : 0.0f,
        material != nullptr ? material->viewportXMax : 1.0f,
        material != nullptr ? material->viewportYMin : 0.0f,
        material != nullptr ? material->viewportYMax : 1.0f,
    };
    const float canvas[2] = {
        material != nullptr ? material->canvasWidth : 1.0f,
        material != nullptr ? material->canvasHeight : 1.0f,
    };
    const int displayEnabled = material != nullptr ? material->displayEnabled : 1;
    std::memcpy(buffer->data() + 80, viewport, sizeof(viewport));
    std::memcpy(buffer->data() + 96, canvas, sizeof(canvas));
    std::memcpy(buffer->data() + 104, &displayEnabled, sizeof(int));
    std::memcpy(buffer->data() + 108, &padding, sizeof(float));
    return true;
}

void ThumbnailAtlasMaterialShader::updateSampledImage(
    RenderState& state,
    int binding,
    QSGTexture** texture,
    QSGMaterial* newMaterial,
    QSGMaterial* oldMaterial
) {
    Q_UNUSED(oldMaterial);
    Q_UNUSED(binding);
    if (texture == nullptr) {
        return;
    }
    auto* material = static_cast<ThumbnailAtlasMaterial*>(newMaterial);
    *texture = material != nullptr ? material->texture : nullptr;
    if (*texture != nullptr) {
        (*texture)->commitTextureOperations(state.rhi(), state.resourceUpdateBatch());
    }
}

void writeThumbnailAtlasVertex(
    ThumbnailAtlasVertex& vertex,
    float worldX,
    float worldY,
    float u,
    float v,
    float offsetX,
    float offsetY,
    float localX,
    float localY,
    float borderX,
    float borderY,
    const QColor& color,
    const QColor& borderColor
) {
    vertex.worldX = worldX;
    vertex.worldY = worldY;
    vertex.u = u;
    vertex.v = v;
    vertex.r = static_cast<float>(color.redF());
    vertex.g = static_cast<float>(color.greenF());
    vertex.b = static_cast<float>(color.blueF());
    vertex.a = static_cast<float>(color.alphaF());
    vertex.offsetX = offsetX;
    vertex.offsetY = offsetY;
    vertex.localX = localX;
    vertex.localY = localY;
    vertex.borderX = borderX;
    vertex.borderY = borderY;
    vertex.borderR = static_cast<float>(borderColor.redF());
    vertex.borderG = static_cast<float>(borderColor.greenF());
    vertex.borderB = static_cast<float>(borderColor.blueF());
    vertex.borderA = static_cast<float>(borderColor.alphaF());
}

void appendThumbnailAtlasQuad(
    QVector<ThumbnailAtlasVertex>& vertices,
    const QPointF& worldCenter,
    double drawWidth,
    double drawHeight,
    const QRectF& uv,
    double borderWidthPx,
    const QColor& color,
    const QColor& borderColor
) {
    const float worldX = static_cast<float>(worldCenter.x());
    const float worldY = static_cast<float>(worldCenter.y());
    const float x0 = static_cast<float>(-drawWidth * 0.5);
    const float y0 = static_cast<float>(-drawHeight * 0.5);
    const float x1 = static_cast<float>(drawWidth * 0.5);
    const float y1 = static_cast<float>(drawHeight * 0.5);
    const float u0 = static_cast<float>(uv.left());
    const float v0 = static_cast<float>(uv.top());
    const float u1 = static_cast<float>(uv.right());
    const float v1 = static_cast<float>(uv.bottom());
    const float borderX = qBound(
        0.0f,
        static_cast<float>(qMax(0.0, borderWidthPx)) / qMax(1.0f, static_cast<float>(drawWidth)),
        0.5f
    );
    const float borderY = qBound(
        0.0f,
        static_cast<float>(qMax(0.0, borderWidthPx)) / qMax(1.0f, static_cast<float>(drawHeight)),
        0.5f
    );
    const int start = vertices.size();
    vertices.resize(start + 6);
    writeThumbnailAtlasVertex(vertices[start], worldX, worldY, u0, v0, x0, y0, 0.0f, 0.0f, borderX, borderY, color, borderColor);
    writeThumbnailAtlasVertex(vertices[start + 1], worldX, worldY, u1, v0, x1, y0, 1.0f, 0.0f, borderX, borderY, color, borderColor);
    writeThumbnailAtlasVertex(vertices[start + 2], worldX, worldY, u1, v1, x1, y1, 1.0f, 1.0f, borderX, borderY, color, borderColor);
    writeThumbnailAtlasVertex(vertices[start + 3], worldX, worldY, u0, v0, x0, y0, 0.0f, 0.0f, borderX, borderY, color, borderColor);
    writeThumbnailAtlasVertex(vertices[start + 4], worldX, worldY, u1, v1, x1, y1, 1.0f, 1.0f, borderX, borderY, color, borderColor);
    writeThumbnailAtlasVertex(vertices[start + 5], worldX, worldY, u0, v1, x0, y1, 0.0f, 1.0f, borderX, borderY, color, borderColor);
}

QRectF rectExpandedForOutsideBorder(const QRectF& rect, double borderWidthPx) {
    const double width = qMax(0.0, borderWidthPx);
    return rect.adjusted(-width, -width, width, width);
}

QRectF uvExpandedForOutsideBorder(const QRectF& rect, const QRectF& uv, double borderWidthPx) {
    const double width = qMax(0.0, borderWidthPx);
    if (width <= 0.0 || rect.width() <= 0.0 || rect.height() <= 0.0) {
        return uv;
    }
    const double uPad = uv.width() * width / rect.width();
    const double vPad = uv.height() * width / rect.height();
    return uv.adjusted(-uPad, -vPad, uPad, vPad);
}

void writeColoredVertex(QSGGeometry::ColoredPoint2D& vertex, float x, float y, const QColor& color);

QVector<QPointF> parsePoints(const QVariantList& pointsList) {
    QVector<QPointF> parsedPoints;
    parsedPoints.reserve(pointsList.size());
    for (const QVariant& pointVar : pointsList) {
        const auto row = pointVar.toList();
        if (row.size() < 2) {
            continue;
        }
        bool okX = false;
        bool okY = false;
        const double x = row.at(0).toDouble(&okX);
        const double y = row.at(1).toDouble(&okY);
        if (!okX || !okY) {
            continue;
        }
        parsedPoints.append(QPointF(x, y));
    }
    return parsedPoints;
}

QVector<float> parseVisibility(const QVariantList& visibilityList, bool& hasVisibility) {
    QVector<float> parsedVisibility;
    hasVisibility = false;
    if (visibilityList.isEmpty()) {
        return parsedVisibility;
    }
    parsedVisibility.reserve(visibilityList.size());
    for (const QVariant& value : visibilityList) {
        parsedVisibility.append(static_cast<float>(value.toDouble()));
    }
    hasVisibility = true;
    return parsedVisibility;
}

QVector<float> parseFloatArray(const QVariantList& values, bool& hasData) {
    QVector<float> parsedValues;
    hasData = false;
    if (values.isEmpty()) {
        return parsedValues;
    }
    parsedValues.reserve(values.size());
    for (const QVariant& value : values) {
        parsedValues.append(static_cast<float>(value.toDouble()));
    }
    hasData = true;
    return parsedValues;
}

QVector<int> parseIntArray(const QVariantList& values) {
    QVector<int> out;
    if (values.isEmpty()) {
        return out;
    }
    out.reserve(values.size());
    for (const QVariant& value : values) {
        out.append(value.toInt());
    }
    return out;
}

QVector<QPointF> parsePointBuffer(const QByteArray& bytes, int pointCount) {
    QVector<QPointF> out;
    const int count = qMax(0, pointCount);
    if (bytes.size() < count * static_cast<int>(sizeof(float)) * 2) {
        return out;
    }
    out.reserve(count);
    const auto* raw = reinterpret_cast<const float*>(bytes.constData());
    for (int index = 0; index < count; ++index) {
        out.append(QPointF(static_cast<double>(raw[index * 2]), static_cast<double>(raw[index * 2 + 1])));
    }
    return out;
}

QVector<int> parseInt64BufferAsInt(const QByteArray& bytes, int pointCount) {
    QVector<int> out;
    const int count = qMax(0, pointCount);
    if (bytes.size() < count * static_cast<int>(sizeof(qint64))) {
        return out;
    }
    out.reserve(count);
    const auto* raw = reinterpret_cast<const qint64*>(bytes.constData());
    for (int index = 0; index < count; ++index) {
        out.append(static_cast<int>(raw[index]));
    }
    return out;
}

QVector<float> parseFloatBuffer(const QByteArray& bytes, int expectedCount, bool& hasData) {
    QVector<float> out;
    hasData = false;
    const int count = qMax(0, expectedCount);
    if (bytes.isEmpty() || bytes.size() < count * static_cast<int>(sizeof(float))) {
        return out;
    }
    out.resize(count);
    std::memcpy(out.data(), bytes.constData(), count * sizeof(float));
    hasData = true;
    return out;
}

QVector<QColor> parseColorBuffer(const QByteArray& bytes, int pointCount, bool& hasColors) {
    QVector<QColor> out;
    hasColors = false;
    const int count = qMax(0, pointCount);
    if (bytes.isEmpty() || bytes.size() < count * 4) {
        return out;
    }
    out.reserve(count);
    if (bytes.size() >= count * 4 * static_cast<int>(sizeof(float))) {
        const auto* raw = reinterpret_cast<const float*>(bytes.constData());
        for (int index = 0; index < count; ++index) {
            const float r = qBound(0.0f, raw[index * 4], 1.0f);
            const float g = qBound(0.0f, raw[index * 4 + 1], 1.0f);
            const float b = qBound(0.0f, raw[index * 4 + 2], 1.0f);
            const float a = qBound(0.0f, raw[index * 4 + 3], 1.0f);
            out.append(QColor::fromRgbF(r, g, b, a));
        }
    } else {
        const auto* raw = reinterpret_cast<const quint8*>(bytes.constData());
        for (int index = 0; index < count; ++index) {
            out.append(QColor(
                raw[index * 4],
                raw[index * 4 + 1],
                raw[index * 4 + 2],
                raw[index * 4 + 3]
            ));
        }
    }
    hasColors = true;
    return out;
}

QVector<QColor> parseColors(const QVariantList& colorList, bool& hasColors) {
    QVector<QColor> parsedColors;
    hasColors = false;
    if (colorList.isEmpty()) {
        return parsedColors;
    }
    parsedColors.reserve(colorList.size());
    for (const QVariant& colorVar : colorList) {
        const auto row = colorVar.toList();
        if (row.size() < 3) {
            parsedColors.append(QColor::fromRgbF(0.12, 0.12, 0.12, 1.0));
            continue;
        }
        const double r = qBound(0.0, row.at(0).toDouble(), 1.0);
        const double g = qBound(0.0, row.at(1).toDouble(), 1.0);
        const double b = qBound(0.0, row.at(2).toDouble(), 1.0);
        const double a = row.size() >= 4 ? qBound(0.0, row.at(3).toDouble(), 1.0) : 1.0;
        parsedColors.append(QColor::fromRgbF(r, g, b, a));
    }
    hasColors = true;
    return parsedColors;
}

bool parseViewport(
    const QVariantList& viewportList,
    double& xMin,
    double& xMax,
    double& yMin,
    double& yMax
) {
    if (viewportList.size() != 4) {
        return false;
    }
    xMin = viewportList.at(0).toDouble();
    xMax = viewportList.at(1).toDouble();
    yMin = viewportList.at(2).toDouble();
    yMax = viewportList.at(3).toDouble();
    return true;
}

int countVisiblePoints(const QVector<QPointF>& points, const QVector<float>& visibility, bool hasVisibility) {
    if (!hasVisibility || visibility.isEmpty()) {
        return points.size();
    }
    const int count = qMin(points.size(), visibility.size());
    int visibleCount = 0;
    for (int index = 0; index < count; ++index) {
        if (visibility.at(index) > 0.0f) {
            ++visibleCount;
        }
    }
    return visibleCount;
}

}  // namespace

DiscoveryRenderItem::DiscoveryRenderItem(QQuickItem* parent)
    : QQuickItem(parent) {
    setFlag(ItemHasContents, true);
}

int DiscoveryRenderItem::rendererApiVersion() const noexcept {
    return 6;
}

bool DiscoveryRenderItem::supportsThumbnailRenderMargin() const noexcept {
    return true;
}

void DiscoveryRenderItem::setLodConfig(bool hybridEnabled, int representativeLimit) {
    const int nextLimit = qBound(1, representativeLimit, 100000);
    const bool changed = !m_lodConfigSet
        || m_hybridRequested != hybridEnabled
        || m_representativePointLimit != nextLimit;
    m_lodConfigSet = true;
    m_hybridRequested = hybridEnabled;
    m_representativePointLimit = nextLimit;
    if (!changed) {
        return;
    }
    // Scene-graph resources are deliberately not touched here.  The render
    // thread consumes this state during updatePaintNode after interaction
    // settling, while pan/wheel frames continue to update camera uniforms only.
    m_pointGeometryDirty = true;
    queueOverviewRasterBuild();
    update();
}

void DiscoveryRenderItem::setStaticPointData(
    const QVariant& points,
    const QVariant& metadataIds,
    const QVariant& visibility,
    const QVariant& colors,
    double pointSize,
    const QVariant& pointSizes,
    const QVariant& symbols,
    const QVariant& edgeColors,
    const QVariant& edgeWidths
) {
    Q_UNUSED(symbols);
    const quint64 generation = ++m_staticBufferGeneration;
    ++m_styleBufferGeneration;
    m_appliedStaticBufferGeneration = generation;
    m_pendingVisibilityBuffer.clear();
    m_hasPendingPointStyleBuffers = false;
    const auto pointsList = points.toList();
    const auto metadataIdsList = metadataIds.toList();
    const auto visibilityList = visibility.toList();
    const auto colorList = colors.toList();
    const auto pointSizesList = pointSizes.toList();
    const auto edgeColorList = edgeColors.toList();
    const auto edgeWidthsList = edgeWidths.toList();

    QVector<QPointF> parsedPoints = parsePoints(pointsList);
    QVector<int> parsedMetadataIds = parseIntArray(metadataIdsList);
    bool hasVisibility = false;
    QVector<float> parsedVisibility = parseVisibility(visibilityList, hasVisibility);
    bool hasColors = false;
    QVector<QColor> parsedColors = parseColors(colorList, hasColors);
    bool hasEdgeColors = false;
    QVector<QColor> parsedEdgeColors = parseColors(edgeColorList, hasEdgeColors);
    bool hasPointSizes = false;
    QVector<float> parsedPointSizes = parseFloatArray(pointSizesList, hasPointSizes);
    bool hasEdgeWidths = false;
    QVector<float> parsedEdgeWidths = parseFloatArray(edgeWidthsList, hasEdgeWidths);

    const int newPointCount = parsedPoints.size();
    const double newPointSize = pointSize > 0.0 ? pointSize : 1.0;

    m_points = std::move(parsedPoints);
    m_metadataIds = std::move(parsedMetadataIds);
    m_pointIndexByMetadataId.clear();
    m_pointIndexByMetadataId.reserve(m_metadataIds.size());
    for (int index = 0; index < m_metadataIds.size(); ++index) {
        m_pointIndexByMetadataId.insert(m_metadataIds.at(index), index);
    }
    m_visibility = std::move(parsedVisibility);
    m_colors = std::move(parsedColors);
    m_edgeColors = std::move(parsedEdgeColors);
    m_pointSizes = std::move(parsedPointSizes);
    m_edgeWidths = std::move(parsedEdgeWidths);
    m_hasVisibilityData = hasVisibility;
    m_hasColorData = hasColors;
    m_hasEdgeColorData = hasEdgeColors;
    m_hasPointSizeData = hasPointSizes;
    m_hasEdgeWidthData = hasEdgeWidths;
    m_pointCount = newPointCount;
    m_pointSize = newPointSize;
    m_pointGeometryDirty = true;
    resetThumbnailAtlasForStaticUpload();
    m_thumbnailNodesDirty = true;
    m_visibleThumbnailMetadataIds.clear();
    queueOverviewRasterBuild();

    emit discoveryDataChanged();
    update();
}

void DiscoveryRenderItem::setStaticPointBuffers(
    const QByteArray& pointsXy,
    int pointCount,
    const QByteArray& metadataIds,
    const QByteArray& visibility,
    const QByteArray& colors,
    double pointSize,
    const QByteArray& pointSizes,
    const QByteArray& symbols,
    const QByteArray& edgeColors,
    const QByteArray& edgeWidths
) {
    Q_UNUSED(symbols);
    const int expectedCount = qMax(0, pointCount);
    const quint64 generation = ++m_staticBufferGeneration;
    ++m_styleBufferGeneration;
    m_hasPendingPointStyleBuffers = false;
    auto* watcher = new QFutureWatcher<ParsedPointBuffers>(this);
    connect(watcher, &QFutureWatcher<ParsedPointBuffers>::finished, this, [this, watcher, generation]() {
        ParsedPointBuffers parsed = watcher->result();
        watcher->deleteLater();
        if (generation != m_staticBufferGeneration) {
            return;
        }
        if (!m_pendingVisibilityBuffer.isEmpty()) {
            bool hasPendingVisibility = false;
            QVector<float> pendingVisibility = parseFloatBuffer(
                m_pendingVisibilityBuffer,
                parsed.pointCount,
                hasPendingVisibility
            );
            if (hasPendingVisibility) {
                parsed.visibility = std::move(pendingVisibility);
                parsed.hasVisibility = true;
            }
            m_pendingVisibilityBuffer.clear();
        }
        m_points = std::move(parsed.points);
        m_metadataIds = std::move(parsed.metadataIds);
        m_pointIndexByMetadataId = std::move(parsed.pointIndexByMetadataId);
        m_visibility = std::move(parsed.visibility);
        m_colors = std::move(parsed.colors);
        m_edgeColors = std::move(parsed.edgeColors);
        m_pointSizes = std::move(parsed.pointSizes);
        m_edgeWidths = std::move(parsed.edgeWidths);
        m_hasVisibilityData = parsed.hasVisibility;
        m_hasColorData = parsed.hasColors;
        m_hasEdgeColorData = parsed.hasEdgeColors;
        m_hasPointSizeData = parsed.hasPointSizes;
        m_hasEdgeWidthData = parsed.hasEdgeWidths;
        m_pointCount = parsed.pointCount;
        m_appliedStaticBufferGeneration = generation;
        m_pointSize = parsed.pointSize;
        m_pointGeometryDirty = true;
        resetThumbnailAtlasForStaticUpload();
        m_thumbnailNodesDirty = true;
        m_visibleThumbnailMetadataIds.clear();
        queueOverviewRasterBuild();
        emit staticUploadFinished(generation, m_pointCount, parsed.preparationMs);
        emit discoveryDataChanged();
        update();
        if (m_hasPendingPointStyleBuffers) {
            const QByteArray colors = m_pendingPointColors;
            const double pointSize = m_pendingPointSize;
            const QByteArray pointSizes = m_pendingPointSizes;
            const QByteArray edgeColors = m_pendingEdgeColors;
            const QByteArray edgeWidths = m_pendingEdgeWidths;
            m_hasPendingPointStyleBuffers = false;
            m_pendingPointColors.clear();
            m_pendingPointSizes.clear();
            m_pendingEdgeColors.clear();
            m_pendingEdgeWidths.clear();
            setPointStyleBuffers(
                colors,
                pointSize,
                pointSizes,
                edgeColors,
                edgeWidths
            );
        }
    });
    watcher->setFuture(QtConcurrent::run(
        [pointsXy, expectedCount, metadataIds, visibility, colors, pointSize, pointSizes, edgeColors, edgeWidths]() {
            QElapsedTimer timer;
            timer.start();
            ParsedPointBuffers parsed;
            parsed.points = parsePointBuffer(pointsXy, expectedCount);
            parsed.metadataIds = parseInt64BufferAsInt(metadataIds, expectedCount);
            parsed.visibility = parseFloatBuffer(visibility, expectedCount, parsed.hasVisibility);
            parsed.colors = parseColorBuffer(colors, expectedCount, parsed.hasColors);
            parsed.edgeColors = parseColorBuffer(edgeColors, expectedCount, parsed.hasEdgeColors);
            parsed.pointSizes = parseFloatBuffer(pointSizes, expectedCount, parsed.hasPointSizes);
            parsed.edgeWidths = parseFloatBuffer(edgeWidths, expectedCount, parsed.hasEdgeWidths);
            parsed.pointIndexByMetadataId.reserve(parsed.metadataIds.size());
            for (int index = 0; index < parsed.metadataIds.size(); ++index) {
                parsed.pointIndexByMetadataId.insert(parsed.metadataIds.at(index), index);
            }
            parsed.pointCount = parsed.points.size();
            parsed.pointSize = pointSize > 0.0 ? pointSize : 1.0;
            parsed.preparationMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
            return parsed;
        }
    ));
}

void DiscoveryRenderItem::setVisibilityBuffer(const QByteArray& visibility) {
    if (m_appliedStaticBufferGeneration != m_staticBufferGeneration) {
        m_pendingVisibilityBuffer = visibility;
        return;
    }
    bool hasVisibility = false;
    QVector<float> parsedVisibility = parseFloatBuffer(visibility, m_pointCount, hasVisibility);
    if (m_pointCount > 0 && !hasVisibility) {
        return;
    }
    m_visibility = std::move(parsedVisibility);
    m_hasVisibilityData = hasVisibility;
    m_pointGeometryDirty = true;
    queueOverviewRasterBuild();
    emit discoveryDataChanged();
    update();
}

void DiscoveryRenderItem::setPointStyleBuffers(
    const QByteArray& colors,
    double pointSize,
    const QByteArray& pointSizes,
    const QByteArray& edgeColors,
    const QByteArray& edgeWidths
) {
    if (m_appliedStaticBufferGeneration != m_staticBufferGeneration) {
        ++m_styleBufferGeneration;
        m_hasPendingPointStyleBuffers = true;
        m_pendingPointColors = colors;
        m_pendingPointSize = pointSize;
        m_pendingPointSizes = pointSizes;
        m_pendingEdgeColors = edgeColors;
        m_pendingEdgeWidths = edgeWidths;
        return;
    }
    const int expectedCount = m_pointCount;
    const quint64 staticGeneration = m_staticBufferGeneration;
    const quint64 styleGeneration = ++m_styleBufferGeneration;
    auto* watcher = new QFutureWatcher<ParsedPointStyleBuffers>(this);
    connect(
        watcher,
        &QFutureWatcher<ParsedPointStyleBuffers>::finished,
        this,
        [this, watcher, staticGeneration, styleGeneration]() {
            ParsedPointStyleBuffers parsed = watcher->result();
            watcher->deleteLater();
            if (
                styleGeneration != m_styleBufferGeneration
                || staticGeneration != m_staticBufferGeneration
                || m_appliedStaticBufferGeneration != m_staticBufferGeneration
            ) {
                return;
            }
            m_colors = std::move(parsed.colors);
            m_edgeColors = std::move(parsed.edgeColors);
            m_pointSizes = std::move(parsed.pointSizes);
            m_edgeWidths = std::move(parsed.edgeWidths);
            m_hasColorData = parsed.hasColors;
            m_hasEdgeColorData = parsed.hasEdgeColors;
            m_hasPointSizeData = parsed.hasPointSizes;
            m_hasEdgeWidthData = parsed.hasEdgeWidths;
            m_pointSize = parsed.pointSize;
            m_pointGeometryDirty = true;
            queueOverviewRasterBuild();
            emit discoveryDataChanged();
            update();
        }
    );
    watcher->setFuture(QtConcurrent::run(
        [colors, pointSize, pointSizes, edgeColors, edgeWidths, expectedCount]() {
            ParsedPointStyleBuffers parsed;
            parsed.colors = parseColorBuffer(
                colors,
                expectedCount,
                parsed.hasColors
            );
            parsed.edgeColors = parseColorBuffer(
                edgeColors,
                expectedCount,
                parsed.hasEdgeColors
            );
            parsed.pointSizes = parseFloatBuffer(
                pointSizes,
                expectedCount,
                parsed.hasPointSizes
            );
            parsed.edgeWidths = parseFloatBuffer(
                edgeWidths,
                expectedCount,
                parsed.hasEdgeWidths
            );
            parsed.pointSize = pointSize > 0.0 ? pointSize : 1.0;
            return parsed;
        }
    ));
}

void DiscoveryRenderItem::setRepresentativeIndexBuffer(const QByteArray& indices) {
    QVector<int> next;
    if (!indices.isEmpty() && indices.size() % static_cast<int>(sizeof(qint32)) == 0) {
        const int count = indices.size() / static_cast<int>(sizeof(qint32));
        next.reserve(count);
        for (int index = 0; index < count; ++index) {
            qint32 rawPointIndex = -1;
            std::memcpy(
                &rawPointIndex,
                indices.constData() + static_cast<qsizetype>(index) * static_cast<qsizetype>(sizeof(qint32)),
                sizeof(qint32)
            );
            const int pointIndex = static_cast<int>(rawPointIndex);
            if (pointIndex >= 0) {
                next.append(pointIndex);
            }
        }
    }
    if (next == m_representativeIndices) {
        return;
    }
    m_representativeIndices = std::move(next);
    m_pointGeometryDirty = true;
    update();
}

void DiscoveryRenderItem::setViewport(const QVariant& viewport) {
    const auto viewportList = viewport.toList();
    double newXMin = m_viewportXMin;
    double newXMax = m_viewportXMax;
    double newYMin = m_viewportYMin;
    double newYMax = m_viewportYMax;
    const bool newHasViewport = parseViewport(viewportList, newXMin, newXMax, newYMin, newYMax);
    if (
        m_hasViewport == newHasViewport
        && sameViewport(m_viewportXMin, m_viewportXMax, m_viewportYMin, m_viewportYMax, newXMin, newXMax, newYMin, newYMax)
    ) {
        return;
    }
    m_hasViewport = newHasViewport;
    if (newHasViewport) {
        m_viewportXMin = newXMin;
        m_viewportXMax = newXMax;
        m_viewportYMin = newYMin;
        m_viewportYMax = newYMax;
    } else {
        m_viewportXMin = 0.0;
        m_viewportXMax = 1.0;
        m_viewportYMin = 0.0;
        m_viewportYMax = 1.0;
    }
    emit discoveryDataChanged();
    update();
}

void DiscoveryRenderItem::setThumbnailLayerConfig(
    bool enabled,
    const QString& variantId,
    double zoomMidThreshold,
    double zoomCloseThreshold,
    double thumbnailSizePx,
    int maxVisibleMid,
    int maxVisibleClose,
    const QString& coloring,
    double borderWidthPx,
    double colorOpacity,
    const QByteArray& colors,
    double renderMarginFraction,
    const QByteArray& borderColors
) {
    const QString nextVariantId = variantId.trimmed();
    QString nextColoring = coloring.trimmed().toLower();
    if (nextColoring != "border" && nextColoring != "tint" && nextColoring != "tint_border") {
        nextColoring = "none";
    }
    const double nextZoomMidThreshold = qMax(1.0, zoomMidThreshold);
    const double nextZoomCloseThreshold = qMax(nextZoomMidThreshold, zoomCloseThreshold);
    const double nextThumbnailSizePx = qMax(1.0, thumbnailSizePx);
    const int nextMaxVisibleMid = qMax(1, maxVisibleMid);
    const int nextMaxVisibleClose = qMax(1, maxVisibleClose);
    const double nextBorderWidthPx = qMax(0.0, borderWidthPx);
    const double nextColorOpacity = qBound(0.0, colorOpacity, 1.0);
    double resolvedRenderMarginFraction = renderMarginFraction;
    if (resolvedRenderMarginFraction < 0.0) {
        bool ok = false;
        resolvedRenderMarginFraction = QString::fromUtf8(
            qgetenv("EIDORA_THUMBNAIL_RENDER_MARGIN_FRACTION")
        ).toDouble(&ok);
        if (!ok) {
            resolvedRenderMarginFraction = 0.0;
        }
    }
    const double nextRenderMarginFraction = qMax(0.0, resolvedRenderMarginFraction);
    bool nextHasThumbnailColors = false;
    QVector<QColor> nextThumbnailColors = parseColorBuffer(colors, m_pointCount, nextHasThumbnailColors);
    bool nextHasThumbnailBorderColors = false;
    QVector<QColor> nextThumbnailBorderColors = parseColorBuffer(
        borderColors,
        m_pointCount,
        nextHasThumbnailBorderColors
    );
    if (
        m_thumbnailLayerEnabled == enabled
        && m_thumbnailVariantId == nextVariantId
        && qFuzzyCompare(m_zoomMidThreshold, nextZoomMidThreshold)
        && qFuzzyCompare(m_zoomCloseThreshold, nextZoomCloseThreshold)
        && qFuzzyCompare(m_thumbnailSizePx, nextThumbnailSizePx)
        && m_maxVisibleMid == nextMaxVisibleMid
        && m_maxVisibleClose == nextMaxVisibleClose
        && m_thumbnailColoring == nextColoring
        && qFuzzyCompare(m_thumbnailBorderWidthPx, nextBorderWidthPx)
        && qFuzzyCompare(m_thumbnailColorOpacity, nextColorOpacity)
        && qFuzzyCompare(m_thumbnailRenderMarginFraction, nextRenderMarginFraction)
        && m_thumbnailColorBuffer == colors
        && m_thumbnailBorderColorBuffer == borderColors
    ) {
        return;
    }
    const bool variantChanged = m_thumbnailVariantId != nextVariantId;
    const bool atlasIdentityChanged = variantChanged;
    m_thumbnailLayerEnabled = enabled;
    m_thumbnailVariantId = nextVariantId;
    m_zoomMidThreshold = nextZoomMidThreshold;
    m_zoomCloseThreshold = nextZoomCloseThreshold;
    m_thumbnailSizePx = nextThumbnailSizePx;
    m_maxVisibleMid = nextMaxVisibleMid;
    m_maxVisibleClose = nextMaxVisibleClose;
    m_thumbnailColoring = nextColoring;
    m_thumbnailBorderWidthPx = nextBorderWidthPx;
    m_thumbnailColorOpacity = nextColorOpacity;
    m_thumbnailRenderMarginFraction = nextRenderMarginFraction;
    m_thumbnailColors = nextThumbnailColors;
    m_thumbnailColorBuffer = colors;
    m_hasThumbnailColors = nextHasThumbnailColors;
    m_thumbnailBorderColors = nextThumbnailBorderColors;
    m_thumbnailBorderColorBuffer = borderColors;
    m_hasThumbnailBorderColors = nextHasThumbnailBorderColors;
    m_thumbnailNodesDirty = true;
    if (atlasIdentityChanged) {
        m_visibleThumbnailMetadataIds.clear();
        m_forcedThumbnailMetadataIds.clear();
        m_thumbnailCache.clear();
        m_thumbnailCacheBytes = 0;
        m_thumbnailSources.clear();
        m_thumbnailDecodeInFlight.clear();
        m_thumbnailSourceDecodeInFlight.clear();
        m_thumbnailSourceWaiters.clear();
        m_thumbnailDecodeRetries.clear();
        m_thumbnailDecodeMissing.clear();
        m_thumbnailDecodeFailed.clear();
        clearAtlas();
        m_atlasVariantId.clear();
        m_atlasTargetPx = 0;
        m_atlasSprites.clear();
        m_thumbnailDrawnCount = 0;
    }
    if (!m_thumbnailLayerEnabled) {
        m_visibleThumbnailMetadataIds.clear();
        m_forcedThumbnailMetadataIds.clear();
        m_thumbnailDrawnCount = 0;
    }
    emitThumbnailStatus();
    update();
}

void DiscoveryRenderItem::setThumbnailPolicy(
    bool enabled,
    const QString& variantId,
    int maxVisibleMid,
    int maxVisibleClose,
    int gpuCacheMb,
    int decodeWorkers
) {
    setThumbnailLayerConfig(
        enabled,
        variantId,
        m_zoomMidThreshold,
        m_zoomCloseThreshold,
        m_thumbnailSizePx,
        maxVisibleMid,
        maxVisibleClose,
        m_thumbnailColoring,
        m_thumbnailBorderWidthPx,
        m_thumbnailColorOpacity,
        m_thumbnailColorBuffer,
        m_thumbnailRenderMarginFraction,
        m_thumbnailBorderColorBuffer
    );
    m_maxThumbnailAtlasBytes = qMax(16, gpuCacheMb) * 1024 * 1024;
    m_thumbnailDecodeWorkers = qBound(1, decodeWorkers, 8);
    trimThumbnailCache();
}

void DiscoveryRenderItem::setThumbnailTileLayerConfig(
    bool enabled,
    const QString& variantId,
    const QString& discoveryCacheId,
    int tileSizePx
) {
    const QString nextVariantId = variantId.trimmed();
    const QString nextDiscoveryCacheId = discoveryCacheId.trimmed();
    const int nextTileSizePx = qMax(64, tileSizePx);
    const bool identityChanged =
        m_tileVariantId != nextVariantId
        || m_tileDiscoveryCacheId != nextDiscoveryCacheId
        || m_tileSizePx != nextTileSizePx;
    m_tileLayerEnabled = enabled;
    m_tileVariantId = nextVariantId;
    m_tileDiscoveryCacheId = nextDiscoveryCacheId;
    m_tileSizePx = nextTileSizePx;
    m_pointGeometryDirty = true;
    if (identityChanged) {
        clearThumbnailTiles();
    }
    update();
}

void DiscoveryRenderItem::setThumbnailTilePolicy(
    int maxCacheMb,
    int decodeWorkers,
    int maxDecodeJobs
) {
    m_maxTileCacheBytes = qMax(16, maxCacheMb) * 1024 * 1024;
    m_tileDecodeWorkers = qMax(1, decodeWorkers);
    m_tileDecodeInFlightLimit = qMax(1, maxDecodeJobs);
    trimTileCache();
}

void DiscoveryRenderItem::setThumbnailTileDisplayKeys(const QVariant& keys) {
    const auto items = keys.toList();
    m_visibleTileHashes.clear();
    m_visibleTileHashes.reserve(items.size());
    m_hasExplicitTileDisplaySet = true;
    QSet<quint64> seen;
    for (const QVariant& rawItem : items) {
        const QVariantMap map = rawItem.toMap();
        if (map.isEmpty()) {
            continue;
        }
        const int lod = map.value("lod").toInt();
        const int tileX = map.value("tile_x").toInt();
        const int tileY = map.value("tile_y").toInt();
        if (lod < 0 || tileX < 0 || tileY < 0) {
            continue;
        }
        TileKey key{lod, tileX, tileY, m_tileVariantId, m_tileDiscoveryCacheId};
        const quint64 hash = tileKeyHash(key);
        if (seen.contains(hash)) {
            continue;
        }
        seen.insert(hash);
        m_visibleTileHashes.append(hash);
    }
    update();
}

void DiscoveryRenderItem::setThumbnailTiles(const QVariant& entries) {
    const auto items = entries.toList();
    bool changed = false;
    for (const QVariant& rawItem : items) {
        const QVariantMap map = rawItem.toMap();
        if (map.isEmpty()) {
            continue;
        }
        const int lod = map.value("lod").toInt();
        const int tileX = map.value("tile_x").toInt();
        const int tileY = map.value("tile_y").toInt();
        const QString path = map.value("path").toString();
        const qint64 mtimeNs = map.value("mtime_ns").toLongLong();
        const double xMin = map.value("x_min").toDouble();
        const double xMax = map.value("x_max").toDouble();
        const double yMin = map.value("y_min").toDouble();
        const double yMax = map.value("y_max").toDouble();
        if (lod < 0 || tileX < 0 || tileY < 0 || path.isEmpty() || xMax <= xMin || yMax <= yMin) {
            continue;
        }
        TileKey key{lod, tileX, tileY, m_tileVariantId, m_tileDiscoveryCacheId};
        const quint64 hash = tileKeyHash(key);
        TileSource source{path, mtimeNs, QRectF(QPointF(xMin, yMin), QPointF(xMax, yMax)).normalized(), lod, tileX, tileY};
        const TileSource previous = m_tileSources.value(hash);
        if (
            previous.path == source.path
            && previous.mtimeNs == source.mtimeNs
            && previous.worldRect == source.worldRect
        ) {
            continue;
        }
        m_tileSources.insert(hash, source);
        m_tileDecodeMissing.remove(hash);
        queueTileDecode(key);
        changed = true;
    }
    if (changed) {
        update();
    }
}

void DiscoveryRenderItem::clearThumbnailTiles() {
    m_tileSources.clear();
    m_tileCache.clear();
    m_tileCacheBytes = 0;
    m_visibleTileHashes.clear();
    m_hasExplicitTileDisplaySet = false;
    m_tileDecodeInFlight.clear();
    m_tileDecodeMissing.clear();
    update();
}

void DiscoveryRenderItem::setVisibleThumbnailMetadataIds(const QVariant& metadataIds) {
    const auto list = metadataIds.toList();
    const QVector<int> nextMetadataIds = parseIntArray(list);
    if (m_visibleThumbnailMetadataIds == nextMetadataIds) {
        return;
    }
    m_visibleThumbnailMetadataIds = nextMetadataIds;
    m_thumbnailNodesDirty = true;
    queueThumbnailDecodes();
    emitThumbnailStatus();
    update();
}

void DiscoveryRenderItem::setHoveredThumbnailMetadataId(int metadataId) {
    const int nextMetadataId = metadataId >= 0 ? metadataId : -1;
    if (m_hoveredThumbnailMetadataId == nextMetadataId) {
        return;
    }
    m_hoveredThumbnailMetadataId = nextMetadataId;
    m_hoveredThumbnailDrawn = false;
    m_hoveredPointGeometryDirty = true;
    m_thumbnailNodesDirty = true;
    update();
}

QVariant DiscoveryRenderItem::missingThumbnailMetadataIds(const QVariant& metadataIds, int targetPx) const {
    QVariantList missing;
    const int normalizedTargetPx = qMax(1, targetPx);
    for (const int metadataId : parseIntArray(metadataIds.toList())) {
        if (!hasThumbnailForMetadata(metadataId, normalizedTargetPx)) {
            missing.append(metadataId);
        }
    }
    return missing;
}

void DiscoveryRenderItem::setThumbnailBatch(const QVariant& entries) {
    const auto items = entries.toList();
    bool changed = false;
    const int currentTargetPx = qMax(1, m_thumbnailDecodeTargetPx);
    for (const QVariant& rawItem : items) {
        const QVariantMap map = rawItem.toMap();
        if (map.isEmpty()) {
            continue;
        }
        const int metadataId = map.value("metadata_id").toInt();
        const int width = map.value("width").toInt();
        const int height = map.value("height").toInt();
        const int targetPx = map.value("target_px").toInt();
        const QByteArray rgba = map.value("rgba").toByteArray();
        if (
            !m_thumbnailLayerEnabled
            || m_thumbnailVariantId.isEmpty()
            || m_thumbnailVariantId == "none"
            || metadataId < 0
            || width <= 0
            || height <= 0
            || targetPx != currentTargetPx
        ) {
            continue;
        }
        if (rgba.size() != width * height * 4) {
            continue;
        }
        QImage image(reinterpret_cast<const uchar*>(rgba.constData()), width, height, width * 4, QImage::Format_RGBA8888);
        if (image.isNull()) {
            continue;
        }
        ThumbnailKey key{
            metadataId,
            targetPx,
            m_thumbnailVariantId,
        };
        upsertThumbnail(key, image.copy());
        changed = true;
    }
    trimThumbnailCache();
    if (changed) {
        m_thumbnailNodesDirty = true;
        m_thumbnailDrawnCount = 0;
        update();
    }
}

void DiscoveryRenderItem::setThumbnailSources(const QString& variantId, const QVariant& entries) {
    Q_UNUSED(variantId);
    Q_UNUSED(entries);
}

void DiscoveryRenderItem::setThumbnailPlan(
    quint64 revision,
    const QVariant& metadataIds,
    const QVariant& forcedMetadataIds,
    const QVariant& planningViewport,
    int displayPx,
    int decodePx
) {
    Q_UNUSED(planningViewport);
    const QVector<int> nextMetadataIds = parseIntArray(metadataIds.toList());
    const QVector<int> forcedIds = parseIntArray(forcedMetadataIds.toList());
    const int nextDisplayPx = qMax(1, displayPx);
    const int nextDecodePx = qMax(1, decodePx);
    const bool geometryChanged =
        m_visibleThumbnailMetadataIds != nextMetadataIds
        || !qFuzzyCompare(m_thumbnailSizePx, static_cast<double>(nextDisplayPx));
    m_thumbnailPlanRevision = revision;
    m_visibleThumbnailMetadataIds = nextMetadataIds;
    m_forcedThumbnailMetadataIds.clear();
    for (const int metadataId : forcedIds) {
        m_forcedThumbnailMetadataIds.insert(metadataId);
    }
    m_thumbnailSizePx = static_cast<double>(nextDisplayPx);
    m_thumbnailDecodeTargetPx = nextDecodePx;
    if (geometryChanged) {
        m_thumbnailNodesDirty = true;
    }
    queueThumbnailDecodes();
    emitThumbnailStatus();
    update();
}

void DiscoveryRenderItem::setThumbnailSourcesV4(
    quint64 revision,
    const QString& variantId,
    const QVariant& entries
) {
    const QString normalizedVariant = variantId.trimmed();
    if (
        revision != m_thumbnailPlanRevision
        || normalizedVariant.isEmpty()
        || normalizedVariant != m_thumbnailVariantId
    ) {
        return;
    }
    const auto items = entries.toList();
    for (const QVariant& rawItem : items) {
        const QVariantMap map = rawItem.toMap();
        const int metadataId = map.value("metadata_id").toInt();
        const int targetPx = qMax(1, map.value("target_px", m_thumbnailDecodeTargetPx).toInt());
        const QString path = map.value("path").toString();
        const bool missing = map.value("missing", false).toBool();
        if (metadataId < 0) {
            continue;
        }
        ThumbnailKey key{metadataId, targetPx, normalizedVariant};
        const quint64 hash = thumbnailKeyHash(key);
        if (missing) {
            m_thumbnailSources.remove(hash);
            m_thumbnailDecodeInFlight.remove(hash);
            m_thumbnailDecodeMissing.insert(hash);
            m_thumbnailDecodeFailed.remove(hash);
            continue;
        }
        if (path.isEmpty()) {
            continue;
        }
        ThumbnailSource source;
        source.key = key;
        source.path = path;
        source.mtimeNs = map.value("mtime_ns").toLongLong();
        source.revision = revision;
        if (m_thumbnailSources.contains(hash)) {
            const ThumbnailSource previous = m_thumbnailSources.value(hash);
            if (previous.path != source.path || previous.mtimeNs != source.mtimeNs) {
                if (m_thumbnailCache.contains(hash)) {
                    m_thumbnailCacheBytes -= m_thumbnailCache.value(hash).bytes;
                    m_thumbnailCache.remove(hash);
                }
                m_atlasPlacements.remove(hash);
                m_thumbnailDecodeRetries.remove(hash);
            }
        }
        m_thumbnailSources.insert(hash, source);
        m_thumbnailDecodeMissing.remove(hash);
        m_thumbnailDecodeFailed.remove(hash);
    }
    queueThumbnailDecodes();
    emitThumbnailStatus();
    update();
}

void DiscoveryRenderItem::setThumbnailRuntimePolicy(
    int cpuCacheMb,
    int gpuCacheMb,
    int atlasPagePx,
    int decodeWorkers,
    int uploadBytesPerFrame,
    double targetFrameMs
) {
    m_maxThumbnailCacheBytes = qMax(16, cpuCacheMb) * 1024 * 1024;
    m_maxThumbnailAtlasBytes = qMax(16, gpuCacheMb) * 1024 * 1024;
    m_atlasSizePx = qBound(512, atlasPagePx, 4096);
    m_thumbnailDecodeWorkers = qBound(1, decodeWorkers, 8);
    m_thumbnailUploadBytesPerFrame = qMax(256 * 1024, uploadBytesPerFrame);
    m_thumbnailTargetFrameMs = qMax(1.0, targetFrameMs);
    trimThumbnailCache();
    queueThumbnailDecodes();
    emitThumbnailStatus();
}

void DiscoveryRenderItem::setThumbnailInteractionActive(bool active) {
    if (m_thumbnailInteractionActive == active) {
        return;
    }
    m_thumbnailInteractionActive = active;
    update();
}

void DiscoveryRenderItem::queueThumbnailDecodes() {
    if (
        !m_thumbnailLayerEnabled
        || m_thumbnailVariantId.isEmpty()
        || m_thumbnailVariantId == "none"
        || m_thumbnailPlanRevision == 0
    ) {
        return;
    }
    for (const int metadataId : m_visibleThumbnailMetadataIds) {
        const ThumbnailKey key{metadataId, m_thumbnailDecodeTargetPx, m_thumbnailVariantId};
        const quint64 hash = thumbnailKeyHash(key);
        if (
            hasThumbnailForMetadata(metadataId, key.targetPx)
            || m_thumbnailDecodeInFlight.contains(hash)
            || m_thumbnailDecodeMissing.contains(hash)
            || m_thumbnailDecodeFailed.contains(hash)
            || !m_thumbnailSources.contains(hash)
        ) {
            continue;
        }
        const ThumbnailSource source = m_thumbnailSources.value(hash);
        const quint64 sourceHash = thumbnailSourceHash(source);
        m_thumbnailDecodeInFlight.insert(hash);
        if (m_thumbnailSourceDecodeInFlight.contains(sourceHash)) {
            m_thumbnailSourceWaiters[sourceHash].append(source);
            continue;
        }
        if (m_thumbnailSourceDecodeInFlight.size() >= m_thumbnailDecodeWorkers) {
            m_thumbnailDecodeInFlight.remove(hash);
            continue;
        }
        m_thumbnailSourceDecodeInFlight.insert(sourceHash);
        m_thumbnailSourceWaiters.insert(sourceHash, QVector<ThumbnailSource>{source});
        QPointer<DiscoveryRenderItem> self(this);
        auto future = QtConcurrent::run([self, sourceHash, source]() {
            QElapsedTimer timer;
            timer.start();
            QImage decoded;
            QFileInfo info(source.path);
            if (info.exists()) {
                QImageReader reader(source.path);
                reader.setAutoTransform(true);
                QSize sourceSize = reader.size();
                if (sourceSize.isValid()) {
                    sourceSize.scale(
                        QSize(source.key.targetPx, source.key.targetPx),
                        Qt::KeepAspectRatio
                    );
                    reader.setScaledSize(sourceSize);
                }
                decoded = reader.read();
                if (!decoded.isNull() && decoded.format() != QImage::Format_RGBA8888) {
                    decoded = decoded.convertToFormat(QImage::Format_RGBA8888);
                }
            }
            const double decodeMs = static_cast<double>(timer.nsecsElapsed()) / 1000000.0;
            if (self == nullptr) {
                return;
            }
            QMetaObject::invokeMethod(self.data(), [self, sourceHash, decoded, decodeMs]() {
                if (self == nullptr) {
                    return;
                }
                self->handleThumbnailSourceDecodeReady(sourceHash, decoded, decodeMs);
            }, Qt::QueuedConnection);
        });
        Q_UNUSED(future);
    }
}

void DiscoveryRenderItem::handleThumbnailSourceDecodeReady(
    quint64 sourceHash,
    const QImage& image,
    double decodeMs
) {
    m_thumbnailSourceDecodeInFlight.remove(sourceHash);
    const QVector<ThumbnailSource> waiters = m_thumbnailSourceWaiters.take(sourceHash);
    m_lastThumbnailDecodeMs = qMax(0.0, decodeMs);
    bool changed = false;
    for (const ThumbnailSource& source : waiters) {
        const quint64 hash = thumbnailKeyHash(source.key);
        m_thumbnailDecodeInFlight.remove(hash);
        if (
            source.key.variantId != m_thumbnailVariantId
            || !m_thumbnailSources.contains(hash)
        ) {
            continue;
        }
        const ThumbnailSource currentSource = m_thumbnailSources.value(hash);
        if (
            currentSource.path != source.path
            || currentSource.mtimeNs != source.mtimeNs
        ) {
            continue;
        }
        if (image.isNull()) {
            const int retryCount = m_thumbnailDecodeRetries.value(hash, 0);
            if (retryCount < 1) {
                m_thumbnailDecodeRetries.insert(hash, retryCount + 1);
                continue;
            }
            m_thumbnailDecodeRetries.remove(hash);
            m_thumbnailDecodeFailed.insert(hash);
            continue;
        }
        upsertThumbnail(source.key, image);
        m_thumbnailDecodeRetries.remove(hash);
        m_thumbnailDecodeFailed.remove(hash);
        changed = true;
    }
    if (changed) {
        m_thumbnailNodesDirty = true;
        trimThumbnailCache();
    }
    queueThumbnailDecodes();
    emitThumbnailStatus();
    emit thumbnailTimingAvailable(m_lastThumbnailDecodeMs, m_lastThumbnailUploadMs);
    update();
}

void DiscoveryRenderItem::emitThumbnailStatus() {
    int resident = 0;
    int queued = 0;
    int decoding = 0;
    int missing = 0;
    int failed = 0;
    for (const int metadataId : m_visibleThumbnailMetadataIds) {
        const ThumbnailKey key{metadataId, m_thumbnailDecodeTargetPx, m_thumbnailVariantId};
        const quint64 hash = thumbnailKeyHash(key);
        if (hasThumbnailForMetadata(metadataId, key.targetPx)) {
            ++resident;
        } else if (m_thumbnailDecodeMissing.contains(hash)) {
            ++missing;
        } else if (m_thumbnailDecodeFailed.contains(hash)) {
            ++failed;
        } else if (m_thumbnailDecodeInFlight.contains(hash)) {
            ++decoding;
        } else {
            ++queued;
        }
    }
    emit thumbnailStatusChanged(
        m_thumbnailPlanRevision,
        m_visibleThumbnailMetadataIds.size(),
        resident,
        m_thumbnailDrawnCount,
        queued,
        decoding,
        missing,
        failed,
        m_atlasPages.size(),
        m_atlasBytes
    );
}

void DiscoveryRenderItem::setThumbnailAtlas(
    const QString& variantId,
    int targetPx,
    const QVariant& pages,
    const QVariant& sprites
) {
    const QString nextVariantId = variantId.trimmed();
    const int nextTargetPx = qMax(1, targetPx);
    const auto pageItems = pages.toList();
    const bool canReusePages = m_atlasVariantId == nextVariantId && m_atlasTargetPx == nextTargetPx;
    m_atlasVariantId = nextVariantId;
    m_atlasTargetPx = nextTargetPx;
    m_atlasSprites.clear();

    if (!canReusePages) {
        m_atlasPlacements.clear();
        clearAtlas();
    }

    for (const QVariant& rawPage : pageItems) {
        const QVariantMap map = rawPage.toMap();
        const int pageIndex = map.value("page_index").toInt();
        const QString path = map.value("path").toString();
        if (pageIndex < 0 || path.isEmpty()) {
            continue;
        }
        bool alreadyLoaded = false;
        for (const AtlasPage& page : m_atlasPages) {
            if (page.pageIndex == pageIndex && page.path == path) {
                alreadyLoaded = true;
                break;
            }
        }
        if (alreadyLoaded) {
            continue;
        }
        QImage image(path);
        if (image.isNull()) {
            continue;
        }
        if (image.format() != QImage::Format_RGBA8888) {
            image = image.convertToFormat(QImage::Format_RGBA8888);
        }
        const int pageBytes = image.width() * image.height() * 4;
        if (m_atlasBytes + pageBytes > m_maxThumbnailAtlasBytes && !m_atlasPages.isEmpty()) {
            continue;
        }
        AtlasPage page;
        page.targetPx = nextTargetPx;
        page.pageIndex = pageIndex;
        page.path = path;
        page.image = image;
        page.bytes = pageBytes;
        page.textureDirty = true;
        page.runtimeWritable = false;
        m_atlasBytes += page.bytes;
        m_atlasPages.append(page);
    }

    const auto spriteItems = sprites.toList();
    for (const QVariant& rawSprite : spriteItems) {
        const QVariantMap map = rawSprite.toMap();
        const int metadataId = map.value("metadata_id").toInt();
        const int pageIndex = map.value("page_index").toInt();
        if (metadataId < 0 || pageIndex < 0) {
            continue;
        }
        AtlasSprite sprite;
        sprite.pageIndex = pageIndex;
        sprite.uv = QRectF(
            map.value("u").toDouble(),
            map.value("v").toDouble(),
            map.value("width").toDouble(),
            map.value("height").toDouble()
        );
        m_atlasSprites.insert(metadataId, sprite);
    }
    m_thumbnailNodesDirty = true;
    update();
}

void DiscoveryRenderItem::setDiscoveryData(
    const QVariant& points,
    const QVariant& metadataIds,
    const QVariant& visibility,
    const QVariant& colors,
    double pointSize,
    const QVariant& pointSizes,
    const QVariant& symbols,
    const QVariant& edgeColors,
    const QVariant& edgeWidths,
    const QVariant& viewport
) {
    setStaticPointData(points, metadataIds, visibility, colors, pointSize, pointSizes, symbols, edgeColors, edgeWidths);
    setViewport(viewport);
}

int DiscoveryRenderItem::pointCount() const noexcept {
    return m_pointCount;
}

bool DiscoveryRenderItem::hasViewport() const noexcept {
    return m_hasViewport;
}

double DiscoveryRenderItem::pointSize() const noexcept {
    return m_pointSize;
}

void DiscoveryRenderItem::queueOverviewRasterBuild() {
    const quint64 generation = ++m_overviewGeneration;
    m_overviewImage = QImage();
    m_overviewWorldRect = QRectF();
    m_overviewTextureDirty = true;
    if (m_lodConfigSet && !m_hybridRequested) {
        update();
        return;
    }
    if (m_points.size() < m_overviewPointThreshold) {
        update();
        return;
    }
    const QVector<QPointF> points = m_points;
    const QVector<float> visibility = m_visibility;
    const QVector<QColor> colors = m_colors;
    const bool hasVisibility = m_hasVisibilityData;
    const bool hasColors = m_hasColorData;
    auto* watcher = new QFutureWatcher<OverviewRasterResult>(this);
    connect(watcher, &QFutureWatcher<OverviewRasterResult>::finished, this, [this, watcher, generation]() {
        const OverviewRasterResult result = watcher->result();
        watcher->deleteLater();
        if (generation != m_overviewGeneration) {
            return;
        }
        m_overviewImage = result.image;
        m_overviewWorldRect = result.worldRect;
        m_overviewTextureDirty = true;
        update();
    });
    watcher->setFuture(QtConcurrent::run(
        [points, visibility, hasVisibility, colors, hasColors]() {
            return buildOverviewRaster(points, visibility, hasVisibility, colors, hasColors);
        }
    ));
}

bool DiscoveryRenderItem::shouldRenderOverview() const {
    if (m_overviewImage.isNull() || m_overviewWorldRect.isEmpty()) {
        return false;
    }
    if (m_forceOverview) {
        return true;
    }
    if (m_lodConfigSet) {
        return m_hybridRequested;
    }
    const double dataArea = m_overviewWorldRect.width() * m_overviewWorldRect.height();
    const double viewportArea = qMax(0.0, m_viewportXMax - m_viewportXMin)
        * qMax(0.0, m_viewportYMax - m_viewportYMin);
    if (dataArea <= 1e-12) {
        return false;
    }
    const double estimatedVisiblePoints = static_cast<double>(m_pointCount)
        * qBound(0.0, viewportArea / dataArea, 1.0);
    const double markerRadius = qMax(0.5, m_pointSize * 0.5) + 1.0;
    constexpr double pi = 3.14159265358979323846;
    const double markerCoverage = estimatedVisiblePoints * pi * markerRadius * markerRadius;
    const double canvasPixels = qMax(1.0, width()) * qMax(1.0, height());
    return estimatedVisiblePoints > static_cast<double>(m_exactVisiblePointLimit)
        || (markerCoverage / canvasPixels) >= 1.2;
}

void DiscoveryRenderItem::syncOverviewNode(QSGNode* rootNode, double widthValue, double heightValue) {
    auto* root = static_cast<DiscoveryRootNode*>(rootNode);
    if (root == nullptr || root->pointOpacity == nullptr) {
        return;
    }
    const bool showOverview = shouldRenderOverview();
    // Hybrid mode deliberately keeps the bounded representative geometry over
    // the density raster.  Exact mode uses the full static geometry.
    root->pointOpacity->setOpacity(1.0);
    root->pointOpacity->markDirty(QSGNode::DirtyOpacity);
    if (!showOverview) {
        if (root->overviewNode != nullptr) {
            root->overviewNode->setRect(QRectF());
        }
        return;
    }
    if (m_overviewTextureDirty && window() != nullptr) {
        auto* replacement = new QSGSimpleTextureNode();
        replacement->setOwnsTexture(true);
        QSGTexture* texture = window()->createTextureFromImage(m_overviewImage);
        if (texture == nullptr) {
            delete replacement;
            return;
        }
        texture->setFiltering(QSGTexture::Linear);
        replacement->setTexture(texture);
        root->insertChildNodeBefore(replacement, root->pointOpacity);
        if (root->overviewNode != nullptr) {
            root->removeChildNode(root->overviewNode);
            delete root->overviewNode;
        }
        root->overviewNode = replacement;
        m_overviewTextureDirty = false;
    }
    const double dx = qMax(1e-12, m_viewportXMax - m_viewportXMin);
    const double dy = qMax(1e-12, m_viewportYMax - m_viewportYMin);
    const double left = ((m_overviewWorldRect.left() - m_viewportXMin) / dx) * widthValue;
    const double top = heightValue - (((m_overviewWorldRect.bottom() - m_viewportYMin) / dy) * heightValue);
    const double rasterWidth = (m_overviewWorldRect.width() / dx) * widthValue;
    const double rasterHeight = (m_overviewWorldRect.height() / dy) * heightValue;
    if (root->overviewNode != nullptr) {
        root->overviewNode->setRect(QRectF(left, top, rasterWidth, rasterHeight));
    }
}

QSGNode* DiscoveryRenderItem::updatePaintNode(QSGNode* oldNode, UpdatePaintNodeData* data) {
    Q_UNUSED(data);
    QElapsedTimer updateTimer;
    updateTimer.start();

    auto* root = static_cast<DiscoveryRootNode*>(oldNode);
    if (root == nullptr) {
        const QSGRendererInterface::GraphicsApi graphicsApi = window() != nullptr
            ? window()->rendererInterface()->graphicsApi()
            : QSGRendererInterface::Unknown;
        // Direct3D has no portable programmable point-size/point-coordinate pair,
        // and Vulkan wide points are an optional device feature. Analytic quads
        // keep marker appearance correct on those backends. OpenGL and Metal have
        // native point sprites and retain the compact one-vertex representation.
        const bool portableQuads = graphicsApi != QSGRendererInterface::OpenGL
            && graphicsApi != QSGRendererInterface::Metal;
        root = new DiscoveryRootNode(portableQuads);
        m_forceOverview = !m_lodConfigSet && portableQuads && m_pointCount > m_exactVisiblePointLimit;
        m_pointGeometryDirty = true;
        m_thumbnailNodesDirty = true;
        m_tileNodes.clear();
        for (AtlasPage& page : m_atlasPages) {
            page.textureDirty = true;
        }
    }

    auto* geometry = root->pointNode != nullptr ? root->pointNode->geometry() : nullptr;
    auto* hoveredGeometry = root->hoveredPointNode != nullptr
        ? root->hoveredPointNode->geometry()
        : nullptr;
    if (geometry == nullptr || hoveredGeometry == nullptr || root->pointTransform == nullptr) {
        return root;
    }
    m_forceOverview = !m_lodConfigSet && root->portableQuads && m_pointCount > m_exactVisiblePointLimit;

    const double widthValue = qMax(1.0, width());
    const double heightValue = qMax(1.0, height());
    const double dx = m_viewportXMax - m_viewportXMin;
    const double dy = m_viewportYMax - m_viewportYMin;
    if (qFuzzyIsNull(dx) || qFuzzyIsNull(dy) || m_points.isEmpty()) {
        geometry->allocate(0);
        root->pointNode->markDirty(QSGNode::DirtyGeometry);
        hoveredGeometry->allocate(0);
        root->hoveredPointNode->markDirty(QSGNode::DirtyGeometry);
        if (root->pointTransform != nullptr) {
            root->pointTransform->setMatrix(QMatrix4x4());
            root->pointTransform->markDirty(QSGNode::DirtyMatrix);
        }
        syncTileNodes(root, widthValue, heightValue, dx, dy);
        syncThumbnailNodes(root, widthValue, heightValue, dx, dy);
        return root;
    }

    if (m_tileLayerEnabled) {
        geometry->allocate(0);
        root->pointNode->markDirty(QSGNode::DirtyGeometry);
        hoveredGeometry->allocate(0);
        root->hoveredPointNode->markDirty(QSGNode::DirtyGeometry);
        root->pointTransform->setMatrix(QMatrix4x4());
        root->pointTransform->markDirty(QSGNode::DirtyMatrix);
        syncTileNodes(root, widthValue, heightValue, dx, dy);
        syncThumbnailNodes(root, widthValue, heightValue, dx, dy);
        m_pointGeometryDirty = true;
        return root;
    }
    if (m_pointGeometryDirty) {
        rebuildPointGeometry(geometry);
        root->pointNode->markDirty(QSGNode::DirtyGeometry);
        m_hoveredPointGeometryDirty = true;
    }
    if (m_hoveredPointGeometryDirty) {
        rebuildHoveredPointGeometry(hoveredGeometry);
        root->hoveredPointNode->markDirty(QSGNode::DirtyGeometry);
    }
    updatePointMaterial(root->pointNode, widthValue, heightValue);
    updatePointMaterial(root->hoveredPointNode, widthValue, heightValue);
    root->pointTransform->setMatrix(QMatrix4x4());
    root->pointTransform->markDirty(QSGNode::DirtyMatrix);
    syncOverviewNode(root, widthValue, heightValue);
    syncTileNodes(root, widthValue, heightValue, dx, dy);
    syncThumbnailNodes(root, widthValue, heightValue, dx, dy);

    emit renderTimingAvailable(static_cast<double>(updateTimer.nsecsElapsed()) / 1000000.0);

    return root;
}

bool DiscoveryRenderItem::sameViewport(
    double xMin,
    double xMax,
    double yMin,
    double yMax,
    double otherXMin,
    double otherXMax,
    double otherYMin,
    double otherYMax
) {
    return qFuzzyCompare(xMin + 1.0, otherXMin + 1.0)
        && qFuzzyCompare(xMax + 1.0, otherXMax + 1.0)
        && qFuzzyCompare(yMin + 1.0, otherYMin + 1.0)
        && qFuzzyCompare(yMax + 1.0, otherYMax + 1.0);
}

void DiscoveryRenderItem::rebuildPointGeometry(
    QSGGeometry* geometry
) {
    const int visiblePointCount = countVisiblePoints(m_points, m_visibility, m_hasVisibilityData);
    const bool representativeMode = m_hybridRequested || m_forceOverview;
    const int representativeLimit = qMax(1, m_representativePointLimit);
    const bool useSpatialRepresentatives = representativeMode && !m_representativeIndices.isEmpty();
    const int pointStride = representativeMode && !useSpatialRepresentatives && visiblePointCount > representativeLimit
        ? qMax(1, static_cast<int>(qCeil(static_cast<double>(visiblePointCount) / representativeLimit)))
        : 1;
    const int spatialCandidateCount = useSpatialRepresentatives
        ? qMin(representativeLimit, m_representativeIndices.size())
        : 0;
    auto spatialPointIndex = [this, spatialCandidateCount](int position) {
        if (spatialCandidateCount <= 1 || m_representativeIndices.size() <= 1) {
            return m_representativeIndices.isEmpty() ? -1 : m_representativeIndices.constFirst();
        }
        const int sourcePosition = static_cast<int>(qRound(
            static_cast<double>(position) * static_cast<double>(m_representativeIndices.size() - 1)
            / static_cast<double>(spatialCandidateCount - 1)
        ));
        return m_representativeIndices.at(qBound(0, sourcePosition, m_representativeIndices.size() - 1));
    };
    int outputPointCount = pointStride > 1
        ? qMin(representativeLimit, static_cast<int>(qCeil(static_cast<double>(visiblePointCount) / pointStride)))
        : visiblePointCount;
    if (useSpatialRepresentatives) {
        outputPointCount = 0;
        for (int position = 0; position < spatialCandidateCount; ++position) {
            const int pointIndex = spatialPointIndex(position);
            if (pointIndex < 0 || pointIndex >= m_points.size()) {
                continue;
            }
            if (m_hasVisibilityData && pointIndex < m_visibility.size() && m_visibility.at(pointIndex) <= 0.0f) {
                continue;
            }
            ++outputPointCount;
        }
    }
    const bool portableQuads = geometry->drawingMode() == QSGGeometry::DrawTriangles;
    if (portableQuads) {
        geometry->allocate(outputPointCount * 4, outputPointCount * 6);
    } else {
        geometry->allocate(outputPointCount);
    }
    auto* pointVertices = portableQuads
        ? nullptr
        : static_cast<PointMarkerVertex*>(geometry->vertexData());
    auto* quadVertices = portableQuads
        ? static_cast<QuadMarkerVertex*>(geometry->vertexData())
        : nullptr;
    auto* quadIndices = portableQuads
        ? static_cast<quint32*>(geometry->indexData())
        : nullptr;
    int outputIndex = 0;
    int visibleIndex = 0;
    const int inputCandidateCount = useSpatialRepresentatives ? spatialCandidateCount : m_points.size();
    for (int candidateIndex = 0; candidateIndex < inputCandidateCount; ++candidateIndex) {
        const int pointIndex = useSpatialRepresentatives ? spatialPointIndex(candidateIndex) : candidateIndex;
        if (pointIndex < 0 || pointIndex >= m_points.size()) {
            continue;
        }
        float visibleAlpha = 1.0f;
        if (m_hasVisibilityData && pointIndex < m_visibility.size()) {
            visibleAlpha = m_visibility.at(pointIndex);
            if (visibleAlpha <= 0.0f) {
                continue;
            }
        }
        const bool includePoint = useSpatialRepresentatives || (visibleIndex % pointStride) == 0;
        ++visibleIndex;
        if (!includePoint || outputIndex >= outputPointCount) {
            continue;
        }
        QColor fillColor = QColor::fromRgbF(0.12, 0.12, 0.12, 1.0);
        if (m_hasColorData && pointIndex < m_colors.size()) {
            fillColor = m_colors.at(pointIndex);
        }
        fillColor.setAlphaF(qBound(0.0f, fillColor.alphaF() * visibleAlpha, 1.0f));
        float radius = static_cast<float>(qMax(1.0, m_pointSize) * 0.5);
        if (m_hasPointSizeData && pointIndex < m_pointSizes.size()) {
            radius = qMax(0.5f, m_pointSizes.at(pointIndex) * 0.5f);
        }
        float edgeWidth = 0.0f;
        if (m_hasEdgeWidthData && pointIndex < m_edgeWidths.size()) {
            edgeWidth = qMax(0.0f, m_edgeWidths.at(pointIndex));
        }
        QColor edgeColor = fillColor;
        if (edgeWidth > 0.0f && m_hasEdgeColorData && pointIndex < m_edgeColors.size()) {
            edgeColor = m_edgeColors.at(pointIndex);
            edgeColor.setAlphaF(qBound(0.0f, edgeColor.alphaF() * visibleAlpha, 1.0f));
        } else {
            edgeWidth = 0.0f;
        }
        const QPointF& point = m_points.at(pointIndex);
        auto writeCommonVertex = [&](auto& vertex) {
            vertex.centerX = static_cast<float>(point.x());
            vertex.centerY = static_cast<float>(point.y());
            vertex.fillR = static_cast<quint8>(fillColor.red());
            vertex.fillG = static_cast<quint8>(fillColor.green());
            vertex.fillB = static_cast<quint8>(fillColor.blue());
            vertex.fillA = static_cast<quint8>(fillColor.alpha());
            vertex.edgeR = static_cast<quint8>(edgeColor.red());
            vertex.edgeG = static_cast<quint8>(edgeColor.green());
            vertex.edgeB = static_cast<quint8>(edgeColor.blue());
            vertex.edgeA = static_cast<quint8>(edgeColor.alpha());
            vertex.radius = radius;
            vertex.edgeWidth = edgeWidth;
        };
        if (!portableQuads) {
            writeCommonVertex(pointVertices[outputIndex]);
        } else {
            constexpr float corners[4][2] = {
                {-1.0f, -1.0f},
                {1.0f, -1.0f},
                {1.0f, 1.0f},
                {-1.0f, 1.0f},
            };
            const quint32 baseVertex = static_cast<quint32>(outputIndex * 4);
            for (int cornerIndex = 0; cornerIndex < 4; ++cornerIndex) {
                QuadMarkerVertex& vertex = quadVertices[baseVertex + cornerIndex];
                writeCommonVertex(vertex);
                vertex.localX = corners[cornerIndex][0];
                vertex.localY = corners[cornerIndex][1];
            }
            const int baseIndex = outputIndex * 6;
            quadIndices[baseIndex] = baseVertex;
            quadIndices[baseIndex + 1] = baseVertex + 1;
            quadIndices[baseIndex + 2] = baseVertex + 2;
            quadIndices[baseIndex + 3] = baseVertex;
            quadIndices[baseIndex + 4] = baseVertex + 2;
            quadIndices[baseIndex + 5] = baseVertex + 3;
        }
        ++outputIndex;
    }
    m_pointGeometryDirty = false;
    geometry->markVertexDataDirty();
    if (portableQuads) {
        geometry->markIndexDataDirty();
    }
}

void DiscoveryRenderItem::rebuildHoveredPointGeometry(QSGGeometry* geometry) {
    if (geometry == nullptr) {
        return;
    }
    const int pointIndex = m_pointIndexByMetadataId.value(m_hoveredThumbnailMetadataId, -1);
    if (
        m_hoveredThumbnailDrawn
        || pointIndex < 0
        || pointIndex >= m_points.size()
        || (m_hasVisibilityData
            && pointIndex < m_visibility.size()
            && m_visibility.at(pointIndex) <= 0.0f)
    ) {
        geometry->allocate(0);
        geometry->markVertexDataDirty();
        m_hoveredPointGeometryDirty = false;
        return;
    }

    QColor fillColor = QColor::fromRgbF(0.12, 0.12, 0.12, 1.0);
    if (m_hasColorData && pointIndex < m_colors.size()) {
        fillColor = m_colors.at(pointIndex);
    }
    if (m_hasVisibilityData && pointIndex < m_visibility.size()) {
        fillColor.setAlphaF(qBound(
            0.0f,
            fillColor.alphaF() * m_visibility.at(pointIndex),
            1.0f
        ));
    }
    float radius = static_cast<float>(qMax(1.0, m_pointSize) * 0.5);
    if (m_hasPointSizeData && pointIndex < m_pointSizes.size()) {
        radius = qMax(0.5f, m_pointSizes.at(pointIndex) * 0.5f);
    }
    radius *= kHoverPointScaleFactor;

    float edgeWidth = 0.0f;
    if (m_hasEdgeWidthData && pointIndex < m_edgeWidths.size()) {
        edgeWidth = qMax(0.0f, m_edgeWidths.at(pointIndex));
    }
    QColor edgeColor = fillColor;
    if (edgeWidth > 0.0f && m_hasEdgeColorData && pointIndex < m_edgeColors.size()) {
        edgeColor = m_edgeColors.at(pointIndex);
        if (m_hasVisibilityData && pointIndex < m_visibility.size()) {
            edgeColor.setAlphaF(qBound(
                0.0f,
                edgeColor.alphaF() * m_visibility.at(pointIndex),
                1.0f
            ));
        }
    } else {
        edgeWidth = 0.0f;
    }

    const QPointF& point = m_points.at(pointIndex);
    const bool portableQuads = geometry->drawingMode() == QSGGeometry::DrawTriangles;
    geometry->allocate(portableQuads ? 4 : 1, portableQuads ? 6 : 0);
    auto writeCommonVertex = [&](auto& vertex) {
        vertex.centerX = static_cast<float>(point.x());
        vertex.centerY = static_cast<float>(point.y());
        vertex.fillR = static_cast<quint8>(fillColor.red());
        vertex.fillG = static_cast<quint8>(fillColor.green());
        vertex.fillB = static_cast<quint8>(fillColor.blue());
        vertex.fillA = static_cast<quint8>(fillColor.alpha());
        vertex.edgeR = static_cast<quint8>(edgeColor.red());
        vertex.edgeG = static_cast<quint8>(edgeColor.green());
        vertex.edgeB = static_cast<quint8>(edgeColor.blue());
        vertex.edgeA = static_cast<quint8>(edgeColor.alpha());
        vertex.radius = radius;
        vertex.edgeWidth = edgeWidth;
    };
    if (!portableQuads) {
        auto* vertex = static_cast<PointMarkerVertex*>(geometry->vertexData());
        writeCommonVertex(vertex[0]);
    } else {
        auto* vertices = static_cast<QuadMarkerVertex*>(geometry->vertexData());
        constexpr float corners[4][2] = {
            {-1.0f, -1.0f},
            {1.0f, -1.0f},
            {1.0f, 1.0f},
            {-1.0f, 1.0f},
        };
        for (int cornerIndex = 0; cornerIndex < 4; ++cornerIndex) {
            writeCommonVertex(vertices[cornerIndex]);
            vertices[cornerIndex].localX = corners[cornerIndex][0];
            vertices[cornerIndex].localY = corners[cornerIndex][1];
        }
        auto* indices = static_cast<quint32*>(geometry->indexData());
        indices[0] = 0;
        indices[1] = 1;
        indices[2] = 2;
        indices[3] = 0;
        indices[4] = 2;
        indices[5] = 3;
        geometry->markIndexDataDirty();
    }
    geometry->markVertexDataDirty();
    m_hoveredPointGeometryDirty = false;
}

void DiscoveryRenderItem::updatePointMaterial(
    QSGGeometryNode* node,
    double widthValue,
    double heightValue
) {
    if (node == nullptr) {
        return;
    }
    auto* material = static_cast<PointMarkerMaterial*>(node->material());
    if (material == nullptr) {
        return;
    }
    material->viewportXMin = static_cast<float>(m_viewportXMin);
    material->viewportXMax = static_cast<float>(m_viewportXMax);
    material->viewportYMin = static_cast<float>(m_viewportYMin);
    material->viewportYMax = static_cast<float>(m_viewportYMax);
    material->itemWidth = static_cast<float>(widthValue);
    material->itemHeight = static_cast<float>(heightValue);
    material->devicePixelRatio = window() != nullptr
        ? static_cast<float>(window()->devicePixelRatio())
        : 1.0f;
    node->markDirty(QSGNode::DirtyMaterial);
}

void DiscoveryRenderItem::syncTileNodes(
    QSGNode* root,
    double widthValue,
    double heightValue,
    double dx,
    double dy
) {
    const bool drawTiles =
        m_tileLayerEnabled
        && !m_tileVariantId.isEmpty()
        && m_tileVariantId != "none"
        && !m_tileDiscoveryCacheId.isEmpty()
        && window() != nullptr
        && !qFuzzyIsNull(dx)
        && !qFuzzyIsNull(dy);
    QSet<quint64> visibleHashes;
    if (drawTiles) {
        QVector<quint64> hashes;
        if (m_hasExplicitTileDisplaySet) {
            hashes.reserve(m_visibleTileHashes.size());
            for (const quint64 hash : m_visibleTileHashes) {
                if (!m_tileSources.contains(hash)) {
                    continue;
                }
                hashes.append(hash);
            }
        } else {
            hashes.reserve(m_tileSources.size());
            for (auto it = m_tileSources.cbegin(); it != m_tileSources.cend(); ++it) {
                hashes.append(it.key());
            }
        }
        QVector<quint64> visibleLoadedHashes;
        visibleLoadedHashes.reserve(hashes.size());
        for (const quint64 hash : hashes) {
            if (!m_tileCache.contains(hash) || m_tileCache.value(hash).image.isNull()) {
                continue;
            }
            const TileSource source = m_tileSources.value(hash);
            const QRectF world = source.worldRect;
            if (
                world.right() < m_viewportXMin
                || world.left() > m_viewportXMax
                || world.bottom() < m_viewportYMin
                || world.top() > m_viewportYMax
            ) {
                continue;
            }
            visibleLoadedHashes.append(hash);
        }
        std::sort(hashes.begin(), hashes.end(), [this](quint64 a, quint64 b) {
            const TileSource left = m_tileSources.value(a);
            const TileSource right = m_tileSources.value(b);
            if (left.lod != right.lod) {
                return left.lod < right.lod;
            }
            if (left.tileY != right.tileY) {
                return left.tileY < right.tileY;
            }
            if (left.tileX != right.tileX) {
                return left.tileX < right.tileX;
            }
            return a < b;
        });
        for (const quint64 hash : hashes) {
            const TileSource source = m_tileSources.value(hash);
            const QRectF world = source.worldRect;
            if (
                world.right() < m_viewportXMin
                || world.left() > m_viewportXMax
                || world.bottom() < m_viewportYMin
                || world.top() > m_viewportYMax
            ) {
                continue;
            }
            bool coveredByHigherLod = false;
            for (const quint64 otherHash : visibleLoadedHashes) {
                if (otherHash == hash) {
                    continue;
                }
                const TileSource other = m_tileSources.value(otherHash);
                if (other.lod <= source.lod) {
                    continue;
                }
                if (other.worldRect.intersects(world)) {
                    coveredByHigherLod = true;
                    break;
                }
            }
            if (coveredByHigherLod) {
                continue;
            }
            if (!m_tileCache.contains(hash) || m_tileCache.value(hash).image.isNull()) {
                TileKey key;
                key.lod = source.lod;
                key.tileX = source.tileX;
                key.tileY = source.tileY;
                key.variantId = m_tileVariantId;
                key.discoveryCacheId = m_tileDiscoveryCacheId;
                queueTileDecode(key);
                continue;
            }
            TileNodeValue nodeValue = m_tileNodes.value(hash);
            if (nodeValue.node == nullptr || m_tileCache.value(hash).textureDirty) {
                if (nodeValue.node != nullptr) {
                    root->removeChildNode(nodeValue.node);
                    delete nodeValue.node;
                    nodeValue.node = nullptr;
                }
                QSGTexture* texture = window()->createTextureFromImage(m_tileCache.value(hash).image);
                if (texture == nullptr) {
                    m_tileNodes.remove(hash);
                    continue;
                }
                nodeValue.node = new QSGSimpleTextureNode();
                nodeValue.node->setTexture(texture);
                nodeValue.node->setOwnsTexture(true);
                root->appendChildNode(nodeValue.node);
                m_tileCache[hash].textureDirty = false;
            }
            const double sxLeft = ((world.left() - m_viewportXMin) / dx) * widthValue;
            const double sxRight = ((world.right() - m_viewportXMin) / dx) * widthValue;
            const double syTop = heightValue - (((world.bottom() - m_viewportYMin) / dy) * heightValue);
            const double syBottom = heightValue - (((world.top() - m_viewportYMin) / dy) * heightValue);
            nodeValue.node->setRect(
                static_cast<float>(sxLeft),
                static_cast<float>(syTop),
                static_cast<float>(sxRight - sxLeft),
                static_cast<float>(syBottom - syTop)
            );
            nodeValue.lastUsedGeneration = m_sceneGeneration;
            m_tileNodes.insert(hash, nodeValue);
            visibleHashes.insert(hash);
        }
    }
    for (auto it = m_tileNodes.begin(); it != m_tileNodes.end();) {
        if (visibleHashes.contains(it.key())) {
            ++it;
            continue;
        }
        if (it.value().node != nullptr) {
            root->removeChildNode(it.value().node);
            delete it.value().node;
        }
        it = m_tileNodes.erase(it);
    }
}

void DiscoveryRenderItem::syncThumbnailNodes(
    QSGNode* root,
    double widthValue,
    double heightValue,
    double dx,
    double dy
) {
    auto* discoveryRoot = static_cast<DiscoveryRootNode*>(root);
    auto setHoveredThumbnailDrawn = [this, discoveryRoot](bool drawn) {
        if (m_hoveredThumbnailDrawn == drawn || discoveryRoot->hoveredPointNode == nullptr) {
            return;
        }
        m_hoveredThumbnailDrawn = drawn;
        QSGGeometry* geometry = discoveryRoot->hoveredPointNode->geometry();
        if (geometry == nullptr) {
            return;
        }
        if (drawn) {
            geometry->allocate(0);
            geometry->markVertexDataDirty();
            m_hoveredPointGeometryDirty = false;
        } else {
            m_hoveredPointGeometryDirty = true;
            rebuildHoveredPointGeometry(geometry);
        }
        discoveryRoot->hoveredPointNode->markDirty(QSGNode::DirtyGeometry);
    };
    if (m_thumbnailAtlasResetPending) {
        // Static coordinate uploads can overlap a thumbnail-plan replay. Retire
        // the old page nodes on the render thread and rebuild runtime pages from
        // the still-resident decoded image cache so no stale texture/UV pairing
        // survives the scene transition.
        releaseThumbnailAtlasNodes(discoveryRoot);
        m_thumbnailAtlasResetPending = false;
    }
    const bool drawThumbnails =
        m_thumbnailLayerEnabled
        && !m_thumbnailVariantId.isEmpty()
        && m_thumbnailVariantId != "none"
        && window() != nullptr;
    ++m_sceneGeneration;
    const bool drawPrebuiltAtlas =
        drawThumbnails
        && m_atlasVariantId == m_thumbnailVariantId
        && !m_atlasPages.isEmpty()
        && !m_atlasSprites.isEmpty();
    const double zoomPxPerWorld = widthValue / qMax(1e-9, dx);
    const bool displayEnabled =
        m_thumbnailPlanRevision > 0
        || zoomPxPerWorld >= m_zoomMidThreshold;
    auto updateMaterialCamera = [this, widthValue, heightValue, displayEnabled](
        QSGGeometryNode* node
    ) {
        if (node == nullptr || node->material() == nullptr) {
            return;
        }
        auto* material = static_cast<ThumbnailAtlasMaterial*>(node->material());
        material->viewportXMin = static_cast<float>(m_viewportXMin);
        material->viewportXMax = static_cast<float>(m_viewportXMax);
        material->viewportYMin = static_cast<float>(m_viewportYMin);
        material->viewportYMax = static_cast<float>(m_viewportYMax);
        material->canvasWidth = static_cast<float>(widthValue);
        material->canvasHeight = static_cast<float>(heightValue);
        material->displayEnabled = displayEnabled ? 1 : 0;
        node->markDirty(QSGNode::DirtyMaterial);
    };
    if (m_thumbnailInteractionActive) {
        for (QSGGeometryNode* node : discoveryRoot->thumbnailAtlasNodes) {
            updateMaterialCamera(node);
        }
        updateMaterialCamera(discoveryRoot->hoveredThumbnailAtlasNode);
        return;
    }
    if (!m_thumbnailNodesDirty) {
        for (QSGGeometryNode* node : discoveryRoot->thumbnailAtlasNodes) {
            updateMaterialCamera(node);
        }
        updateMaterialCamera(discoveryRoot->hoveredThumbnailAtlasNode);
        return;
    }
    QVector<int> candidateIndices;
    if (drawThumbnails && !m_visibleThumbnailMetadataIds.isEmpty()) {
        candidateIndices.reserve(m_visibleThumbnailMetadataIds.size());
        for (const int metadataId : m_visibleThumbnailMetadataIds) {
            if (!m_pointIndexByMetadataId.contains(metadataId)) {
                continue;
            }
            const int index = m_pointIndexByMetadataId.value(metadataId);
            if (index < 0 || index >= m_points.size()) {
                continue;
            }
            if (m_hasVisibilityData && index < m_visibility.size() && m_visibility.at(index) <= 0.0f) {
                continue;
            }
            candidateIndices.append(index);
        }
    }
    const int hoveredPointIndex = m_pointIndexByMetadataId.value(
        m_hoveredThumbnailMetadataId,
        -1
    );
    const int hoveredCandidatePosition = candidateIndices.indexOf(hoveredPointIndex);
    if (hoveredCandidatePosition >= 0) {
        candidateIndices.removeAt(hoveredCandidatePosition);
        candidateIndices.append(hoveredPointIndex);
    }
    if (!drawThumbnails || qFuzzyIsNull(dx) || qFuzzyIsNull(dy) || candidateIndices.isEmpty()) {
        setHoveredThumbnailDrawn(false);
        releaseThumbnailAtlasNodes(discoveryRoot);
        if (discoveryRoot->hoveredPointNode->parent() == root) {
            root->removeChildNode(discoveryRoot->hoveredPointNode);
        }
        root->appendChildNode(discoveryRoot->hoveredPointNode);
        m_thumbnailDrawnCount = 0;
        m_thumbnailNodesDirty = false;
        emitThumbnailStatus();
        return;
    }
    prepareRuntimeAtlasCapacity();
    QHash<int, int> pageIndexToVectorIndex;
    pageIndexToVectorIndex.reserve(m_atlasPages.size());
    for (int pageVectorIndex = 0; pageVectorIndex < m_atlasPages.size(); ++pageVectorIndex) {
        const int pageIndex = m_atlasPages.at(pageVectorIndex).pageIndex;
        if (pageIndex >= 0) {
            pageIndexToVectorIndex.insert(pageIndex, pageVectorIndex);
        }
    }

    QVector<QVector<ThumbnailAtlasVertex>> pageVertices(m_atlasPages.size());
    QVector<ThumbnailAtlasVertex> hoveredThumbnailVertices;
    int hoveredThumbnailPageVectorIndex = -1;
    const int displayTargetPx = qMax(1, static_cast<int>(std::round(m_thumbnailSizePx)));
    QVector<int> lookupTargets{128, 64, 32};
    const int requestedTarget = qMax(1, m_thumbnailDecodeTargetPx);
    if (!lookupTargets.contains(requestedTarget)) {
        lookupTargets.append(requestedTarget);
        std::sort(lookupTargets.begin(), lookupTargets.end(), [](int left, int right) {
            return left > right;
        });
    }

    for (const int i : candidateIndices) {
        if (i >= m_metadataIds.size()) {
            break;
        }
        const int metadataId = m_metadataIds.at(i);
        const bool isHoveredThumbnail = metadataId >= 0 && metadataId == m_hoveredThumbnailMetadataId;
        const QPointF& point = m_points.at(i);

        int pageVectorIndex = -1;
        QRectF uv;
        double drawWidth = static_cast<double>(displayTargetPx);
        double drawHeight = static_cast<double>(displayTargetPx);
        if (drawPrebuiltAtlas && m_atlasSprites.contains(metadataId)) {
            const AtlasSprite sprite = m_atlasSprites.value(metadataId);
            if (pageIndexToVectorIndex.contains(sprite.pageIndex)) {
                pageVectorIndex = pageIndexToVectorIndex.value(sprite.pageIndex);
                uv = sprite.uv;
                if (pageVectorIndex >= 0 && pageVectorIndex < m_atlasPages.size()) {
                    const AtlasPage& page = m_atlasPages.at(pageVectorIndex);
                    const double spriteWidth = qMax(1.0, sprite.uv.width() * static_cast<double>(page.image.width()));
                    const double spriteHeight = qMax(1.0, sprite.uv.height() * static_cast<double>(page.image.height()));
                    if (spriteWidth >= spriteHeight) {
                        drawHeight = drawWidth * spriteHeight / spriteWidth;
                    } else {
                        drawWidth = drawHeight * spriteWidth / spriteHeight;
                    }
                }
            }
        }
        if (pageVectorIndex < 0) {
            for (const int lookupTarget : lookupTargets) {
                ThumbnailKey key{metadataId, lookupTarget, m_thumbnailVariantId};
                const quint64 hash = thumbnailKeyHash(key);
                if (m_atlasPlacements.contains(hash)) {
                    AtlasPlacement placement = m_atlasPlacements.value(hash);
                    if (placement.pageIndex >= 0 && placement.pageIndex < m_atlasPages.size()) {
                        placement.generation = ++m_thumbnailGeneration;
                        m_atlasPlacements.insert(hash, placement);
                        pageVectorIndex = placement.pageIndex;
                        uv = placement.uv;
                        if (placement.width >= placement.height) {
                            drawHeight = drawWidth * static_cast<double>(placement.height)
                                / qMax(1.0, static_cast<double>(placement.width));
                        } else {
                            drawWidth = drawHeight * static_cast<double>(placement.width)
                                / qMax(1.0, static_cast<double>(placement.height));
                        }
                    }
                } else if (m_thumbnailCache.contains(hash) && !m_thumbnailCache.value(hash).image.isNull()) {
                    if (ensureAtlasPlacement(hash, key, m_thumbnailCache.value(hash).image)) {
                        const AtlasPlacement placement = m_atlasPlacements.value(hash);
                        if (placement.pageIndex >= 0 && placement.pageIndex < m_atlasPages.size()) {
                            pageVectorIndex = placement.pageIndex;
                            uv = placement.uv;
                            if (placement.width >= placement.height) {
                                drawHeight = drawWidth * static_cast<double>(placement.height)
                                    / qMax(1.0, static_cast<double>(placement.width));
                            } else {
                                drawWidth = drawHeight * static_cast<double>(placement.width)
                                    / qMax(1.0, static_cast<double>(placement.height));
                            }
                        }
                    }
                }
                if (pageVectorIndex >= 0) {
                    break;
                }
            }
        }
        if (pageVectorIndex < 0 || pageVectorIndex >= m_atlasPages.size()) {
            continue;
        }
        if (pageVectorIndex >= pageVertices.size()) {
            pageVertices.resize(pageVectorIndex + 1);
        }
        if (isHoveredThumbnail || m_forcedThumbnailMetadataIds.contains(metadataId)) {
            drawWidth *= isHoveredThumbnail ? kHoverThumbnailScaleFactor : 1.0;
            drawHeight *= isHoveredThumbnail ? kHoverThumbnailScaleFactor : 1.0;
        }
        QRectF atlasUv = uv;
        double renderWidth = drawWidth;
        double renderHeight = drawHeight;
        const bool drawsBorder = (
            (m_thumbnailColoring == "border" || m_thumbnailColoring == "tint_border")
            && m_hasThumbnailBorderColors
            && m_thumbnailBorderWidthPx > 0.0
        );
        if (drawsBorder) {
            const QRectF logicalRect(0.0, 0.0, drawWidth, drawHeight);
            atlasUv = uvExpandedForOutsideBorder(logicalRect, uv, m_thumbnailBorderWidthPx);
            renderWidth += m_thumbnailBorderWidthPx * 2.0;
            renderHeight += m_thumbnailBorderWidthPx * 2.0;
        }
        QColor thumbnailColor = QColor::fromRgbF(1.0, 1.0, 1.0, 1.0);
        if (
            (m_thumbnailColoring == "tint" || m_thumbnailColoring == "tint_border")
            && m_hasThumbnailColors
            && i >= 0
            && i < m_thumbnailColors.size()
        ) {
            thumbnailColor = m_thumbnailColors.at(i);
        }
        QColor thumbnailBorderColor = QColor::fromRgbF(1.0, 1.0, 1.0, 1.0);
        if (
            drawsBorder
            && i >= 0
            && i < m_thumbnailBorderColors.size()
        ) {
            thumbnailBorderColor = m_thumbnailBorderColors.at(i);
        }
        QVector<ThumbnailAtlasVertex>& targetVertices = isHoveredThumbnail
            ? hoveredThumbnailVertices
            : pageVertices[pageVectorIndex];
        if (isHoveredThumbnail) {
            hoveredThumbnailPageVectorIndex = pageVectorIndex;
        }
        appendThumbnailAtlasQuad(
            targetVertices,
            point,
            renderWidth,
            renderHeight,
            atlasUv,
            drawsBorder ? m_thumbnailBorderWidthPx : 0.0,
            thumbnailColor,
            thumbnailBorderColor
        );
    }

    releaseHoveredThumbnailAtlasNode(discoveryRoot);
    while (discoveryRoot->thumbnailAtlasNodes.size() > m_atlasPages.size()) {
        QSGGeometryNode* node = discoveryRoot->thumbnailAtlasNodes.takeLast();
        if (node != nullptr) {
            if (node->parent() != nullptr) {
                node->parent()->removeChildNode(node);
            }
            delete node;
        }
    }
    if (discoveryRoot->thumbnailAtlasNodes.size() < m_atlasPages.size()) {
        discoveryRoot->thumbnailAtlasNodes.resize(m_atlasPages.size());
    }

    QElapsedTimer uploadTimer;
    uploadTimer.start();
    int uploadedBytes = 0;
    bool pendingTextureUpload = false;
    m_thumbnailDrawnCount = 0;
    for (int pageVectorIndex = 0; pageVectorIndex < m_atlasPages.size(); ++pageVectorIndex) {
        AtlasPage& page = m_atlasPages[pageVectorIndex];
        QSGGeometryNode*& node = discoveryRoot->thumbnailAtlasNodes[pageVectorIndex];
        int dirtyBytes = 0;
        if (node == nullptr) {
            dirtyBytes = page.bytes;
        } else {
            for (const QRect& rect : page.dirtyRects) {
                dirtyBytes += rect.width() * rect.height() * 4;
            }
        }
        const bool uploadAllowed =
            !page.textureDirty
            || uploadedBytes == 0
            || uploadedBytes + dirtyBytes <= m_thumbnailUploadBytesPerFrame;
        if (page.textureDirty && !uploadAllowed) {
            pendingTextureUpload = true;
        }
        if (node == nullptr) {
            auto* texture = new ThumbnailAtlasTexture(page.image);
            if (texture == nullptr) {
                continue;
            }
            texture->setFiltering(QSGTexture::Linear);
            texture->setHorizontalWrapMode(QSGTexture::ClampToEdge);
            texture->setVerticalWrapMode(QSGTexture::ClampToEdge);
            node = new QSGGeometryNode();
            auto* geometry = new QSGGeometry(thumbnailAtlasAttributes(), 0);
            geometry->setDrawingMode(QSGGeometry::DrawTriangles);
            node->setGeometry(geometry);
            node->setFlag(QSGNode::OwnsGeometry, true);
            auto* material = new ThumbnailAtlasMaterial(texture);
            node->setMaterial(material);
            node->setFlag(QSGNode::OwnsMaterial, true);
            if (node->parent() != root) {
                root->appendChildNode(node);
            }
            page.textureDirty = false;
            page.dirtyRects.clear();
            uploadedBytes += dirtyBytes;
        } else if (page.textureDirty && uploadAllowed) {
            auto* material = static_cast<ThumbnailAtlasMaterial*>(node->material());
            auto* texture = material != nullptr
                ? static_cast<ThumbnailAtlasTexture*>(material->texture)
                : nullptr;
            if (texture != nullptr) {
                for (const QRect& rect : page.dirtyRects) {
                    texture->queueRegion(rect, page.image.copy(rect));
                }
                page.textureDirty = false;
                page.dirtyRects.clear();
                uploadedBytes += dirtyBytes;
                node->markDirty(QSGNode::DirtyMaterial);
            }
        }
        auto* material = static_cast<ThumbnailAtlasMaterial*>(node->material());
        if (material != nullptr) {
            const bool hasTint = (
                (m_thumbnailColoring == "tint" || m_thumbnailColoring == "tint_border")
                && m_hasThumbnailColors
            );
            const bool hasBorder = (
                (m_thumbnailColoring == "border" || m_thumbnailColoring == "tint_border")
                && m_hasThumbnailBorderColors
            );
            material->colorMode = hasTint && hasBorder ? 3 : (hasTint ? 2 : (hasBorder ? 1 : 0));
            material->tintStrength = 1.0f;
            material->bodyOpacity = static_cast<float>(m_thumbnailColorOpacity);
        }
        updateMaterialCamera(node);
        auto* geometry = node->geometry();
        const QVector<ThumbnailAtlasVertex>& vertices =
            pageVectorIndex < pageVertices.size() ? pageVertices.at(pageVectorIndex) : QVector<ThumbnailAtlasVertex>();
        if (!page.textureDirty) {
            m_thumbnailDrawnCount += vertices.size() / 6;
        }
        geometry->allocate(vertices.size());
        if (!vertices.isEmpty()) {
            std::memcpy(geometry->vertexData(), vertices.constData(), vertices.size() * sizeof(ThumbnailAtlasVertex));
        }
        node->markDirty(QSGNode::DirtyGeometry | QSGNode::DirtyMaterial);
    }
    if (discoveryRoot->hoveredPointNode->parent() == root) {
        root->removeChildNode(discoveryRoot->hoveredPointNode);
    }
    root->appendChildNode(discoveryRoot->hoveredPointNode);
    bool hoveredThumbnailDrawn = false;
    if (
        hoveredThumbnailPageVectorIndex >= 0
        && hoveredThumbnailPageVectorIndex < discoveryRoot->thumbnailAtlasNodes.size()
        && !hoveredThumbnailVertices.isEmpty()
    ) {
        QSGGeometryNode* sourceNode = discoveryRoot->thumbnailAtlasNodes.at(
            hoveredThumbnailPageVectorIndex
        );
        auto* sourceMaterial = sourceNode != nullptr
            ? static_cast<ThumbnailAtlasMaterial*>(sourceNode->material())
            : nullptr;
        if (sourceMaterial != nullptr && sourceMaterial->texture != nullptr) {
            auto* hoveredNode = new QSGGeometryNode();
            auto* hoveredGeometry = new QSGGeometry(
                thumbnailAtlasAttributes(),
                hoveredThumbnailVertices.size()
            );
            hoveredGeometry->setDrawingMode(QSGGeometry::DrawTriangles);
            std::memcpy(
                hoveredGeometry->vertexData(),
                hoveredThumbnailVertices.constData(),
                hoveredThumbnailVertices.size() * sizeof(ThumbnailAtlasVertex)
            );
            hoveredNode->setGeometry(hoveredGeometry);
            hoveredNode->setFlag(QSGNode::OwnsGeometry, true);
            hoveredNode->setMaterial(sourceMaterial);
            discoveryRoot->hoveredThumbnailAtlasNode = hoveredNode;
            root->appendChildNode(hoveredNode);
            hoveredThumbnailDrawn = true;
            ++m_thumbnailDrawnCount;
        }
    }
    setHoveredThumbnailDrawn(hoveredThumbnailDrawn);
    m_lastThumbnailUploadMs = uploadedBytes > 0
        ? static_cast<double>(uploadTimer.nsecsElapsed()) / 1000000.0
        : 0.0;
    m_thumbnailNodesDirty = pendingTextureUpload;
    emit thumbnailTimingAvailable(m_lastThumbnailDecodeMs, m_lastThumbnailUploadMs);
    emitThumbnailStatus();
    if (pendingTextureUpload) {
        update();
    }
}

void DiscoveryRenderItem::queueTileDecode(const TileKey& key) {
    const quint64 hash = tileKeyHash(key);
    if (
        m_tileCache.contains(hash)
        || m_tileDecodeInFlight.contains(hash)
        || m_tileDecodeMissing.contains(hash)
        || !m_tileSources.contains(hash)
        || m_tileDecodeInFlight.size() >= m_tileDecodeInFlightLimit
    ) {
        return;
    }
    const TileSource source = m_tileSources.value(hash);
    if (source.path.isEmpty()) {
        m_tileDecodeMissing.insert(hash);
        return;
    }
    m_tileDecodeInFlight.insert(hash);
    QPointer<DiscoveryRenderItem> self(this);
    auto future = QtConcurrent::run([self, key, source]() {
        QImage decoded;
        QFileInfo info(source.path);
        if (info.exists()) {
            QImageReader reader(source.path);
            reader.setAutoTransform(true);
            decoded = reader.read();
            if (!decoded.isNull()) {
                decoded = decoded.convertToFormat(QImage::Format_RGBA8888);
            }
        }
        if (self == nullptr) {
            return;
        }
        QMetaObject::invokeMethod(self.data(), [self, key, decoded]() {
            if (self == nullptr) {
                return;
            }
            self->handleTileDecodeReady(key, decoded);
        }, Qt::QueuedConnection);
    });
    Q_UNUSED(future);
}

void DiscoveryRenderItem::handleTileDecodeReady(const TileKey& key, const QImage& image) {
    const quint64 hash = tileKeyHash(key);
    m_tileDecodeInFlight.remove(hash);
    if (image.isNull() || !m_tileSources.contains(hash)) {
        m_tileDecodeMissing.insert(hash);
        return;
    }
    upsertTile(key, m_tileSources.value(hash), image);
    trimTileCache();
    update();
}

void DiscoveryRenderItem::upsertTile(const TileKey& key, const TileSource& source, const QImage& image) {
    const quint64 hash = tileKeyHash(key);
    const int bytes = image.width() * image.height() * 4;
    if (m_tileCache.contains(hash)) {
        m_tileCacheBytes -= m_tileCache.value(hash).bytes;
    }
    TileValue value;
    value.image = image;
    value.worldRect = source.worldRect;
    value.bytes = bytes;
    value.generation = ++m_thumbnailGeneration;
    value.textureDirty = true;
    m_tileCache.insert(hash, value);
    m_tileCacheBytes += bytes;
}

bool DiscoveryRenderItem::hasThumbnailForMetadata(int metadataId, int targetPx) const {
    if (
        !m_thumbnailLayerEnabled
        || m_thumbnailVariantId.isEmpty()
        || m_thumbnailVariantId == "none"
        || metadataId < 0
        || targetPx <= 0
    ) {
        return false;
    }
    QVector<int> acceptableTargets{targetPx};
    for (const int tier : {32, 64, 128}) {
        if (tier >= targetPx && !acceptableTargets.contains(tier)) {
            acceptableTargets.append(tier);
        }
    }
    for (const int acceptableTarget : acceptableTargets) {
        const ThumbnailKey key{metadataId, acceptableTarget, m_thumbnailVariantId};
        const quint64 hash = thumbnailKeyHash(key);
        if (m_atlasPlacements.contains(hash)) {
            const AtlasPlacement placement = m_atlasPlacements.value(hash);
            if (placement.pageIndex >= 0 && placement.pageIndex < m_atlasPages.size()) {
                return true;
            }
        }
        if (m_thumbnailCache.contains(hash) && !m_thumbnailCache.value(hash).image.isNull()) {
            return true;
        }
    }
    if (
        m_atlasVariantId == m_thumbnailVariantId
        && m_atlasTargetPx >= targetPx
        && m_atlasSprites.contains(metadataId)
    ) {
        const AtlasSprite sprite = m_atlasSprites.value(metadataId);
        for (const AtlasPage& page : m_atlasPages) {
            if (page.pageIndex == sprite.pageIndex) {
                return true;
            }
        }
    }
    return false;
}

bool DiscoveryRenderItem::ensureAtlasPlacement(quint64 hash, const ThumbnailKey& key, const QImage& image) {
    if (m_atlasPlacements.contains(hash)) {
        const AtlasPlacement existing = m_atlasPlacements.value(hash);
        if (existing.pageIndex >= 0 && existing.pageIndex < m_atlasPages.size()) {
            return true;
        }
        m_atlasPlacements.remove(hash);
    }
    if (image.isNull()) {
        return false;
    }
    const int width = qMax(1, image.width());
    const int height = qMax(1, image.height());
    const int pageIndex = findOrCreateAtlasPage(key.targetPx, width, height);
    if (pageIndex < 0 || pageIndex >= m_atlasPages.size()) {
        return false;
    }
    AtlasPage& page = m_atlasPages[pageIndex];
    if (page.nextX + width > page.image.width()) {
        page.nextX = 0;
        page.nextY += page.rowHeight;
        page.rowHeight = 0;
    }
    if (page.nextY + height > page.image.height()) {
        return false;
    }
    QPainter painter(&page.image);
    painter.setCompositionMode(QPainter::CompositionMode_Source);
    painter.drawImage(page.nextX, page.nextY, image);
    painter.end();

    const QRectF uv(
        static_cast<double>(page.nextX) / static_cast<double>(page.image.width()),
        static_cast<double>(page.nextY) / static_cast<double>(page.image.height()),
        static_cast<double>(width) / static_cast<double>(page.image.width()),
        static_cast<double>(height) / static_cast<double>(page.image.height())
    );
    AtlasPlacement placement;
    placement.pageIndex = pageIndex;
    placement.uv = uv;
    placement.width = width;
    placement.height = height;
    placement.metadataId = key.metadataId;
    placement.targetPx = key.targetPx;
    placement.variantId = key.variantId;
    placement.generation = ++m_thumbnailGeneration;
    m_atlasPlacements.insert(hash, placement);

    page.dirtyRects.append(QRect(page.nextX, page.nextY, width, height));
    page.nextX += width;
    page.rowHeight = qMax(page.rowHeight, height);
    page.textureDirty = true;
    return true;
}

void DiscoveryRenderItem::prepareRuntimeAtlasCapacity() {
    if (m_visibleThumbnailMetadataIds.isEmpty() || m_atlasPages.isEmpty()) {
        return;
    }
    int requiredTargetPx = 0;
    int requiredWidth = 0;
    int requiredHeight = 0;
    for (const int metadataId : m_visibleThumbnailMetadataIds) {
        const int targets[] = {m_thumbnailDecodeTargetPx, 64, 32};
        for (const int targetPx : targets) {
            if (targetPx > m_thumbnailDecodeTargetPx || targetPx <= 0) {
                continue;
            }
            const ThumbnailKey key{metadataId, targetPx, m_thumbnailVariantId};
            const quint64 hash = thumbnailKeyHash(key);
            if (
                m_atlasPlacements.contains(hash)
                || !m_thumbnailCache.contains(hash)
                || m_thumbnailCache.value(hash).image.isNull()
            ) {
                continue;
            }
            const QImage& image = m_thumbnailCache.value(hash).image;
            requiredTargetPx = targetPx;
            requiredWidth = image.width();
            requiredHeight = image.height();
            break;
        }
        if (requiredWidth > 0 && requiredHeight > 0) {
            break;
        }
    }
    if (requiredWidth <= 0 || requiredHeight <= 0) {
        return;
    }
    for (const AtlasPage& page : m_atlasPages) {
        if (!page.runtimeWritable || page.targetPx != requiredTargetPx) {
            continue;
        }
        int nextX = page.nextX;
        int nextY = page.nextY;
        int rowHeight = page.rowHeight;
        if (nextX + requiredWidth > page.image.width()) {
            nextX = 0;
            nextY += rowHeight;
            rowHeight = 0;
        }
        if (nextY + requiredHeight <= page.image.height()) {
            return;
        }
    }
    const int pageSize = qMax(m_atlasSizePx, qMax(requiredWidth, requiredHeight));
    const int requiredPageBytes = pageSize * pageSize * 4;
    if (m_atlasBytes + requiredPageBytes <= m_maxThumbnailAtlasBytes) {
        return;
    }

    const QSet<int> activeIds(
        m_visibleThumbnailMetadataIds.cbegin(),
        m_visibleThumbnailMetadataIds.cend()
    );
    struct PageEvictionCandidate {
        int oldIndex = -1;
        int activeCount = 0;
        quint64 lastUsedGeneration = 0;
        int bytes = 0;
    };
    QVector<PageEvictionCandidate> candidates;
    for (int index = 0; index < m_atlasPages.size(); ++index) {
        const AtlasPage& page = m_atlasPages.at(index);
        if (!page.runtimeWritable) {
            continue;
        }
        PageEvictionCandidate candidate;
        candidate.oldIndex = index;
        candidate.bytes = page.bytes;
        for (auto it = m_atlasPlacements.cbegin(); it != m_atlasPlacements.cend(); ++it) {
            const AtlasPlacement& placement = it.value();
            if (placement.pageIndex != index) {
                continue;
            }
            candidate.lastUsedGeneration = qMax(
                candidate.lastUsedGeneration,
                placement.generation
            );
            if (activeIds.contains(placement.metadataId)) {
                ++candidate.activeCount;
            }
        }
        candidates.append(candidate);
    }
    std::sort(
        candidates.begin(),
        candidates.end(),
        [](const PageEvictionCandidate& lhs, const PageEvictionCandidate& rhs) {
            if (lhs.activeCount != rhs.activeCount) {
                return lhs.activeCount < rhs.activeCount;
            }
            return lhs.lastUsedGeneration < rhs.lastUsedGeneration;
        }
    );
    QSet<int> removedPageIndices;
    int remainingBytes = m_atlasBytes;
    for (const PageEvictionCandidate& candidate : candidates) {
        if (remainingBytes + requiredPageBytes <= m_maxThumbnailAtlasBytes) {
            break;
        }
        removedPageIndices.insert(candidate.oldIndex);
        remainingBytes -= candidate.bytes;
    }
    if (removedPageIndices.isEmpty()) {
        return;
    }

    QVector<AtlasPage> keptPages;
    keptPages.reserve(m_atlasPages.size() - removedPageIndices.size());
    QHash<int, int> indexRemap;
    for (int oldIndex = 0; oldIndex < m_atlasPages.size(); ++oldIndex) {
        if (removedPageIndices.contains(oldIndex)) {
            continue;
        }
        indexRemap.insert(oldIndex, keptPages.size());
        AtlasPage kept = m_atlasPages.at(oldIndex);
        kept.textureDirty = true;
        kept.dirtyRects = {
            QRect(0, 0, kept.image.width(), kept.image.height()),
        };
        keptPages.append(kept);
    }
    for (auto it = m_atlasPlacements.begin(); it != m_atlasPlacements.end();) {
        if (!indexRemap.contains(it.value().pageIndex)) {
            it = m_atlasPlacements.erase(it);
            continue;
        }
        AtlasPlacement placement = it.value();
        placement.pageIndex = indexRemap.value(placement.pageIndex);
        it.value() = placement;
        ++it;
    }
    m_atlasPages = keptPages;
    m_atlasBytes = qMax(0, remainingBytes);
}

int DiscoveryRenderItem::findOrCreateAtlasPage(int targetPx, int imageWidth, int imageHeight) {
    for (int index = 0; index < m_atlasPages.size(); ++index) {
        AtlasPage& page = m_atlasPages[index];
        if (page.targetPx != targetPx || !page.runtimeWritable) {
            continue;
        }
        int nextX = page.nextX;
        int nextY = page.nextY;
        int rowHeight = page.rowHeight;
        if (nextX + imageWidth > page.image.width()) {
            nextX = 0;
            nextY += rowHeight;
            rowHeight = 0;
        }
        if (nextY + imageHeight <= page.image.height()) {
            return index;
        }
    }
    const int pageSize = qMax(m_atlasSizePx, qMax(imageWidth, imageHeight));
    const int pageBytes = pageSize * pageSize * 4;
    if (m_atlasBytes + pageBytes > m_maxThumbnailAtlasBytes && !m_atlasPages.isEmpty()) {
        return -1;
    }
    AtlasPage page;
    page.targetPx = targetPx;
    page.image = QImage(pageSize, pageSize, QImage::Format_RGBA8888);
    page.image.fill(Qt::transparent);
    page.bytes = pageBytes;
    page.runtimeWritable = true;
    m_atlasBytes += pageBytes;
    m_atlasPages.append(page);
    return m_atlasPages.size() - 1;
}

void DiscoveryRenderItem::clearAtlas() {
    m_atlasPages.clear();
    m_atlasBytes = 0;
    m_atlasPlacements.clear();
}

void DiscoveryRenderItem::clearRuntimeAtlas() {
    if (m_atlasPages.isEmpty() && m_atlasPlacements.isEmpty()) {
        return;
    }
    QVector<AtlasPage> keptPages;
    keptPages.reserve(m_atlasPages.size());
    QHash<int, int> pageIndexRemap;
    int bytes = 0;
    for (const AtlasPage& page : m_atlasPages) {
        if (page.runtimeWritable) {
            continue;
        }
        const int nextIndex = keptPages.size();
        pageIndexRemap.insert(page.pageIndex, nextIndex);
        AtlasPage kept = page;
        kept.pageIndex = nextIndex;
        keptPages.append(kept);
        bytes += kept.bytes;
    }
    QHash<int, AtlasSprite> keptSprites;
    for (auto it = m_atlasSprites.cbegin(); it != m_atlasSprites.cend(); ++it) {
        AtlasSprite sprite = it.value();
        if (!pageIndexRemap.contains(sprite.pageIndex)) {
            continue;
        }
        sprite.pageIndex = pageIndexRemap.value(sprite.pageIndex);
        keptSprites.insert(it.key(), sprite);
    }
    m_atlasPages = keptPages;
    m_atlasBytes = bytes;
    m_atlasSprites = keptSprites;
    m_atlasPlacements.clear();
}

void DiscoveryRenderItem::resetThumbnailAtlasForStaticUpload() {
    // Keep decoded thumbnails reusable. Only page packing is tied to the scene
    // transition and must be reconstructed before the next thumbnail draw.
    clearRuntimeAtlas();
    m_thumbnailAtlasResetPending = true;
}

quint64 DiscoveryRenderItem::thumbnailKeyHash(const ThumbnailKey& key) {
    return qHash(key.variantId) ^ (static_cast<quint64>(key.metadataId) << 24) ^ static_cast<quint64>(key.targetPx);
}

quint64 DiscoveryRenderItem::thumbnailSourceHash(const ThumbnailSource& source) {
    quint64 hash = qHash(source.path);
    hash ^= static_cast<quint64>(qHash(source.key.variantId)) << 1;
    hash ^= static_cast<quint64>(source.key.targetPx & 0xffff) << 48;
    hash ^= static_cast<quint64>(source.mtimeNs);
    return hash;
}

quint64 DiscoveryRenderItem::tileKeyHash(const TileKey& key) {
    quint64 hash = qHash(key.variantId) ^ (qHash(key.discoveryCacheId) << 1);
    hash ^= (static_cast<quint64>(key.lod) & 0xff) << 56;
    hash ^= (static_cast<quint64>(key.tileX) & 0x0fffffff) << 28;
    hash ^= (static_cast<quint64>(key.tileY) & 0x0fffffff);
    return hash;
}

void DiscoveryRenderItem::upsertThumbnail(const ThumbnailKey& key, const QImage& image) {
    const quint64 hash = thumbnailKeyHash(key);
    const int bytes = image.width() * image.height() * 4;
    if (m_thumbnailCache.contains(hash)) {
        m_thumbnailCacheBytes -= m_thumbnailCache.value(hash).bytes;
    }
    ThumbnailValue value;
    value.image = image;
    value.bytes = bytes;
    value.generation = ++m_thumbnailGeneration;
    value.textureDirty = true;
    m_thumbnailCache.insert(hash, value);
    m_thumbnailCacheBytes += bytes;
}

void DiscoveryRenderItem::trimTileCache() {
    if (m_tileCacheBytes <= m_maxTileCacheBytes) {
        return;
    }
    while (m_tileCacheBytes > m_maxTileCacheBytes && !m_tileCache.isEmpty()) {
        quint64 oldestKey = 0;
        quint64 oldestGen = std::numeric_limits<quint64>::max();
        for (auto it = m_tileCache.cbegin(); it != m_tileCache.cend(); ++it) {
            if (it.value().generation < oldestGen) {
                oldestGen = it.value().generation;
                oldestKey = it.key();
            }
        }
        if (!m_tileCache.contains(oldestKey)) {
            break;
        }
        m_tileCacheBytes -= m_tileCache.value(oldestKey).bytes;
        m_tileCache.remove(oldestKey);
        if (m_tileNodes.contains(oldestKey)) {
            m_tileNodes[oldestKey].lastUsedGeneration = 0;
        }
    }
}

void DiscoveryRenderItem::trimThumbnailCache() {
    if (m_thumbnailCacheBytes <= m_maxThumbnailCacheBytes) {
        return;
    }
    while (m_thumbnailCacheBytes > m_maxThumbnailCacheBytes && !m_thumbnailCache.isEmpty()) {
        quint64 oldestKey = 0;
        quint64 oldestGen = std::numeric_limits<quint64>::max();
        for (auto it = m_thumbnailCache.cbegin(); it != m_thumbnailCache.cend(); ++it) {
            if (it.value().generation < oldestGen) {
                oldestGen = it.value().generation;
                oldestKey = it.key();
            }
        }
        if (!m_thumbnailCache.contains(oldestKey)) {
            break;
        }
        m_thumbnailCacheBytes -= m_thumbnailCache.value(oldestKey).bytes;
        m_thumbnailCache.remove(oldestKey);
    }
}
