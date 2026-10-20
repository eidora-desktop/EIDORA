#pragma once

#include <QQuickPaintedItem>
#include <QVariant>
#include <QVector>
#include <QPointF>
#include <QStringList>
#include <QImage>

class DiscoveryPinOverlayItem : public QQuickPaintedItem {
    Q_OBJECT

public:
    explicit DiscoveryPinOverlayItem(QQuickItem* parent = nullptr);

    Q_INVOKABLE void setPinData(
        const QVariant& viewport,
        const QVariant& pins,
        const QVariant& style,
        bool visible
    );
    Q_INVOKABLE void setViewport(const QVariant& viewport);

    void paint(QPainter* painter) override;

private:
    struct PinEntry {
        QPointF worldPoint;
        QPointF boxWorldAnchor;
        QPointF screenOffset;
        QStringList lines;
        QImage thumbnail;
        int thumbnailSize = 96;
        bool hasBoxWorldAnchor = false;
    };

    bool m_hasViewport = false;
    double m_xMin = 0.0;
    double m_xMax = 1.0;
    double m_yMin = 0.0;
    double m_yMax = 1.0;
    QVector<PinEntry> m_pins;
    bool m_visible = true;
    QString m_fontFamily = QStringLiteral("Arial");
    int m_fontSize = 11;
    QColor m_textColor = QColor("#222222");
    QColor m_boxColor = QColor::fromRgbF(0.94, 0.94, 0.94, 0.96);
    QColor m_borderColor = QColor::fromRgbF(0.2, 0.2, 0.2, 0.9);
    QColor m_connectorColor = QColor::fromRgbF(0.2, 0.2, 0.2, 0.9);
    double m_connectorWidth = 1.5;
    double m_borderWidth = 1.0;
    bool m_drawBox = true;
    bool m_drawConnector = true;
    bool m_drawText = true;
    bool m_showLabel = true;
    bool m_showValue = true;
    bool m_labelBold = false;
    bool m_labelItalic = false;
    bool m_labelUnderline = false;
    bool m_valueBold = false;
    bool m_valueItalic = false;
    bool m_valueUnderline = false;
    bool m_drawThumbnail = false;
    QString m_thumbnailPosition = QStringLiteral("left");
    QString m_connectorTarget = QStringLiteral("center");
};
