#pragma once

#include <QQuickPaintedItem>
#include <QColor>
#include <QVariant>
#include <QVector>
#include <QPointF>

class DiscoveryLassoOverlayItem : public QQuickPaintedItem {
    Q_OBJECT

public:
    explicit DiscoveryLassoOverlayItem(QQuickItem* parent = nullptr);

    Q_INVOKABLE void setLassoData(
        const QVariant& viewport,
        const QVariant& polygons,
        const QVariant& polygonColors,
        double fillOpacity,
        const QVariant& currentPolygon,
        int selectedPolygonIndex
    );
    Q_INVOKABLE void setViewport(const QVariant& viewport);
    Q_INVOKABLE void setDrawFill(bool drawFill);

    void paint(QPainter* painter) override;

private:
    bool m_hasViewport = false;
    double m_xMin = 0.0;
    double m_xMax = 1.0;
    double m_yMin = 0.0;
    double m_yMax = 1.0;
    QVector<QVector<QPointF>> m_polygons;
    QVector<QColor> m_polygonColors;
    double m_fillOpacity = 1.0;
    QVector<QPointF> m_currentPolygon;
    int m_selectedPolygonIndex = -1;
    bool m_drawFill = true;
};
