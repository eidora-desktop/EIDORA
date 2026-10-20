#pragma once

#include <QQuickItem>
#include <QVariant>
#include <QVector>
#include <QPointF>
#include <QString>
#include <QByteArray>
#include <QSGNode>
#include <QHash>
#include <QImage>
#include <QSet>
#include <QRectF>

class QSGTexture;
class QSGGeometry;
class QSGGeometryNode;
class QSGSimpleTextureNode;

class DiscoveryRenderItem : public QQuickItem {
    Q_OBJECT
    Q_PROPERTY(int pointCount READ pointCount NOTIFY discoveryDataChanged)
    Q_PROPERTY(bool hasViewport READ hasViewport NOTIFY discoveryDataChanged)
    Q_PROPERTY(double pointSize READ pointSize NOTIFY discoveryDataChanged)
public:
    explicit DiscoveryRenderItem(QQuickItem* parent = nullptr);

    Q_INVOKABLE int rendererApiVersion() const noexcept;
    Q_INVOKABLE bool supportsThumbnailRenderMargin() const noexcept;
    Q_INVOKABLE void setLodConfig(bool hybridEnabled, int representativeLimit);

    Q_INVOKABLE void setStaticPointData(
        const QVariant& points,
        const QVariant& metadataIds,
        const QVariant& visibility,
        const QVariant& colors,
        double pointSize,
        const QVariant& pointSizes,
        const QVariant& symbols,
        const QVariant& edgeColors,
        const QVariant& edgeWidths
    );
    Q_INVOKABLE void setStaticPointBuffers(
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
    );
    Q_INVOKABLE void setVisibilityBuffer(const QByteArray& visibility);
    Q_INVOKABLE void setPointStyleBuffers(
        const QByteArray& colors,
        double pointSize,
        const QByteArray& pointSizes,
        const QByteArray& edgeColors,
        const QByteArray& edgeWidths
    );
    Q_INVOKABLE void setRepresentativeIndexBuffer(const QByteArray& indices);
    Q_INVOKABLE void setViewport(const QVariant& viewport);
    Q_INVOKABLE void setThumbnailLayerConfig(
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
        double renderMarginFraction = -1.0,
        const QByteArray& borderColors = QByteArray()
    );
    Q_INVOKABLE void setThumbnailBatch(const QVariant& entries);
    Q_INVOKABLE void setThumbnailSources(const QString& variantId, const QVariant& entries);
    Q_INVOKABLE void setThumbnailPlan(
        quint64 revision,
        const QVariant& metadataIds,
        const QVariant& forcedMetadataIds,
        const QVariant& planningViewport,
        int displayPx,
        int decodePx
    );
    Q_INVOKABLE void setThumbnailSourcesV4(
        quint64 revision,
        const QString& variantId,
        const QVariant& entries
    );
    Q_INVOKABLE void setThumbnailRuntimePolicy(
        int cpuCacheMb,
        int gpuCacheMb,
        int atlasPagePx,
        int decodeWorkers,
        int uploadBytesPerFrame,
        double targetFrameMs
    );
    Q_INVOKABLE void setThumbnailInteractionActive(bool active);
    Q_INVOKABLE QVariant missingThumbnailMetadataIds(const QVariant& metadataIds, int targetPx) const;
    Q_INVOKABLE void setThumbnailAtlas(
        const QString& variantId,
        int targetPx,
        const QVariant& pages,
        const QVariant& sprites
    );
    Q_INVOKABLE void setThumbnailPolicy(
        bool enabled,
        const QString& variantId,
        int maxVisibleMid,
        int maxVisibleClose,
        int gpuCacheMb,
        int decodeWorkers
    );
    Q_INVOKABLE void setThumbnailTileLayerConfig(
        bool enabled,
        const QString& variantId,
        const QString& discoveryCacheId,
        int tileSizePx
    );
    Q_INVOKABLE void setThumbnailTilePolicy(
        int maxCacheMb,
        int decodeWorkers,
        int maxDecodeJobs
    );
    Q_INVOKABLE void setThumbnailTileDisplayKeys(const QVariant& keys);
    Q_INVOKABLE void setThumbnailTiles(const QVariant& entries);
    Q_INVOKABLE void clearThumbnailTiles();
    Q_INVOKABLE void setVisibleThumbnailMetadataIds(const QVariant& metadataIds);
    Q_INVOKABLE void setHoveredThumbnailMetadataId(int metadataId);

    Q_INVOKABLE void setDiscoveryData(
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
    );

    [[nodiscard]] int pointCount() const noexcept;
    [[nodiscard]] bool hasViewport() const noexcept;
    [[nodiscard]] double pointSize() const noexcept;

protected:
    QSGNode* updatePaintNode(QSGNode* oldNode, UpdatePaintNodeData* data) override;

signals:
    void discoveryDataChanged();
    void renderTimingAvailable(double updateMs);
    void staticUploadFinished(quint64 generation, int pointCount, double preparationMs);
    void thumbnailStatusChanged(
        quint64 revision,
        int desired,
        int resident,
        int drawn,
        int queued,
        int decoding,
        int missing,
        int failed,
        int atlasPages,
        int atlasBytes
    );
    void thumbnailTimingAvailable(double decodeMs, double uploadMs);

private:
    struct ThumbnailKey {
        int metadataId = -1;
        int targetPx = 0;
        QString variantId;
    };
    struct ThumbnailValue {
        QImage image;
        int bytes = 0;
        quint64 generation = 0;
        bool textureDirty = true;
    };
    struct ThumbnailSource {
        ThumbnailKey key;
        QString path;
        qint64 mtimeNs = 0;
        quint64 revision = 0;
    };
    struct AtlasPlacement {
        int pageIndex = -1;
        QRectF uv;
        int width = 0;
        int height = 0;
        int metadataId = -1;
        int targetPx = 0;
        QString variantId;
        quint64 generation = 0;
    };
    struct AtlasPage {
        int targetPx = 0;
        int pageIndex = -1;
        QString path;
        QImage image;
        bool textureDirty = true;
        int nextX = 0;
        int nextY = 0;
        int rowHeight = 0;
        int bytes = 0;
        bool runtimeWritable = true;
        QVector<QRect> dirtyRects;
    };
    struct AtlasSprite {
        int pageIndex = -1;
        QRectF uv;
    };
    struct TileKey {
        int lod = 0;
        int tileX = 0;
        int tileY = 0;
        QString variantId;
        QString discoveryCacheId;
    };
    struct TileSource {
        QString path;
        qint64 mtimeNs = 0;
        QRectF worldRect;
        int lod = 0;
        int tileX = 0;
        int tileY = 0;
    };
    struct TileValue {
        QImage image;
        QRectF worldRect;
        int bytes = 0;
        quint64 generation = 0;
        bool textureDirty = true;
    };
    struct TileNodeValue {
        QSGSimpleTextureNode* node = nullptr;
        quint64 lastUsedGeneration = 0;
    };
    static quint64 thumbnailKeyHash(const ThumbnailKey& key);
    static quint64 thumbnailSourceHash(const ThumbnailSource& source);
    static quint64 tileKeyHash(const TileKey& key);
    static bool sameViewport(double xMin, double xMax, double yMin, double yMax, double otherXMin, double otherXMax, double otherYMin, double otherYMax);
    void upsertThumbnail(const ThumbnailKey& key, const QImage& image);
    void trimThumbnailCache();
    bool hasThumbnailForMetadata(int metadataId, int targetPx) const;
    bool ensureAtlasPlacement(quint64 hash, const ThumbnailKey& key, const QImage& image);
    int findOrCreateAtlasPage(int targetPx, int imageWidth, int imageHeight);
    void prepareRuntimeAtlasCapacity();
    void queueThumbnailDecodes();
    void handleThumbnailSourceDecodeReady(
        quint64 sourceHash,
        const QImage& image,
        double decodeMs
    );
    void emitThumbnailStatus();
    void clearAtlas();
    void clearRuntimeAtlas();
    void resetThumbnailAtlasForStaticUpload();
    void queueOverviewRasterBuild();
    bool shouldRenderOverview() const;
    void syncOverviewNode(QSGNode* root, double widthValue, double heightValue);
    void rebuildPointGeometry(QSGGeometry* geometry);
    void rebuildHoveredPointGeometry(QSGGeometry* geometry);
    void updatePointMaterial(QSGGeometryNode* node, double widthValue, double heightValue);
    void syncThumbnailNodes(QSGNode* root, double widthValue, double heightValue, double dx, double dy);
    void syncTileNodes(QSGNode* root, double widthValue, double heightValue, double dx, double dy);
    void queueTileDecode(const TileKey& key);
    void handleTileDecodeReady(const TileKey& key, const QImage& image);
    void upsertTile(const TileKey& key, const TileSource& source, const QImage& image);
    void trimTileCache();

    QVector<QPointF> m_points;
    QVector<int> m_metadataIds;
    QVector<float> m_visibility;
    QVector<QColor> m_colors;
    QVector<QColor> m_edgeColors;
    QVector<float> m_pointSizes;
    QVector<float> m_edgeWidths;
    QVector<int> m_representativeIndices;
    bool m_hasColorData = false;
    bool m_hasEdgeColorData = false;
    bool m_hasPointSizeData = false;
    bool m_hasEdgeWidthData = false;
    bool m_hasVisibilityData = false;
    int m_pointCount = 0;
    bool m_hasViewport = false;
    double m_pointSize = 1.0;
    double m_viewportXMin = 0.0;
    double m_viewportXMax = 1.0;
    double m_viewportYMin = 0.0;
    double m_viewportYMax = 1.0;
    bool m_thumbnailLayerEnabled = false;
    QString m_thumbnailVariantId = "none";
    double m_zoomMidThreshold = 90.0;
    double m_zoomCloseThreshold = 200.0;
    double m_thumbnailSizePx = 64.0;
    int m_maxVisibleMid = 50000;
    int m_maxVisibleClose = 250000;
    QString m_thumbnailColoring = QStringLiteral("none");
    double m_thumbnailBorderWidthPx = 2.0;
    double m_thumbnailColorOpacity = 0.75;
    double m_thumbnailRenderMarginFraction = 0.0;
    QVector<QColor> m_thumbnailColors;
    QByteArray m_thumbnailColorBuffer;
    bool m_hasThumbnailColors = false;
    QVector<QColor> m_thumbnailBorderColors;
    QByteArray m_thumbnailBorderColorBuffer;
    bool m_hasThumbnailBorderColors = false;
    int m_maxThumbnailCacheBytes = 128 * 1024 * 1024;
    int m_maxThumbnailAtlasBytes = 96 * 1024 * 1024;
    int m_thumbnailCacheBytes = 0;
    quint64 m_thumbnailGeneration = 0;
    quint64 m_sceneGeneration = 0;
    quint64 m_staticBufferGeneration = 0;
    quint64 m_appliedStaticBufferGeneration = 0;
    quint64 m_styleBufferGeneration = 0;
    QByteArray m_pendingVisibilityBuffer;
    bool m_hasPendingPointStyleBuffers = false;
    QByteArray m_pendingPointColors;
    double m_pendingPointSize = 1.0;
    QByteArray m_pendingPointSizes;
    QByteArray m_pendingEdgeColors;
    QByteArray m_pendingEdgeWidths;
    QHash<quint64, ThumbnailValue> m_thumbnailCache;
    QHash<quint64, ThumbnailSource> m_thumbnailSources;
    QSet<quint64> m_thumbnailDecodeInFlight;
    QSet<quint64> m_thumbnailSourceDecodeInFlight;
    QHash<quint64, QVector<ThumbnailSource>> m_thumbnailSourceWaiters;
    QHash<quint64, int> m_thumbnailDecodeRetries;
    QSet<quint64> m_thumbnailDecodeMissing;
    QSet<quint64> m_thumbnailDecodeFailed;
    QVector<int> m_visibleThumbnailMetadataIds;
    QSet<int> m_forcedThumbnailMetadataIds;
    quint64 m_thumbnailPlanRevision = 0;
    int m_thumbnailDecodeTargetPx = 32;
    int m_thumbnailDecodeWorkers = 2;
    int m_thumbnailUploadBytesPerFrame = 4 * 1024 * 1024;
    double m_thumbnailTargetFrameMs = 12.0;
    double m_lastThumbnailDecodeMs = 0.0;
    double m_lastThumbnailUploadMs = 0.0;
    int m_thumbnailDrawnCount = 0;
    int m_hoveredThumbnailMetadataId = -1;
    QHash<int, int> m_pointIndexByMetadataId;
    QHash<quint64, AtlasPlacement> m_atlasPlacements;
    QVector<AtlasPage> m_atlasPages;
    QString m_atlasVariantId;
    int m_atlasTargetPx = 0;
    QHash<int, AtlasSprite> m_atlasSprites;
    bool m_tileLayerEnabled = false;
    QString m_tileVariantId = "none";
    QString m_tileDiscoveryCacheId;
    int m_tileSizePx = 512;
    int m_maxTileCacheBytes = 512 * 1024 * 1024;
    int m_tileCacheBytes = 0;
    int m_tileDecodeWorkers = 2;
    int m_tileDecodeInFlightLimit = 8;
    QHash<quint64, TileSource> m_tileSources;
    QHash<quint64, TileValue> m_tileCache;
    QHash<quint64, TileNodeValue> m_tileNodes;
    QVector<quint64> m_visibleTileHashes;
    bool m_hasExplicitTileDisplaySet = false;
    QSet<quint64> m_tileDecodeInFlight;
    QSet<quint64> m_tileDecodeMissing;
    bool m_pointGeometryDirty = true;
    bool m_hoveredPointGeometryDirty = true;
    bool m_hoveredThumbnailDrawn = false;
    QImage m_overviewImage;
    QRectF m_overviewWorldRect;
    bool m_overviewTextureDirty = false;
    quint64 m_overviewGeneration = 0;
    int m_overviewPointThreshold = 25000;
    int m_exactVisiblePointLimit = 100000;
    int m_representativePointLimit = 60000;
    bool m_lodConfigSet = false;
    bool m_hybridRequested = false;
    bool m_forceOverview = false;
    bool m_thumbnailNodesDirty = true;
    bool m_thumbnailAtlasResetPending = false;
    bool m_thumbnailInteractionActive = false;
    int m_atlasSizePx = 2048;
    int m_atlasBytes = 0;
};
