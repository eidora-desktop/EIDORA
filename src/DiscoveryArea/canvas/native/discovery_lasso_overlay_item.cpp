#include "discovery_lasso_overlay_item.h"

#include <QBrush>
#include <QColor>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QtMath>

namespace {

bool parseViewport(
    const QVariant& viewport,
    double& xMin,
    double& xMax,
    double& yMin,
    double& yMax
) {
    const QVariantList list = viewport.toList();
    if (list.size() != 4) {
        return false;
    }
    xMin = list.at(0).toDouble();
    xMax = list.at(1).toDouble();
    yMin = list.at(2).toDouble();
    yMax = list.at(3).toDouble();
    return true;
}

QVector<QPointF> parsePointList(const QVariant& pointsVariant) {
    QVector<QPointF> out;
    const QVariantList points = pointsVariant.toList();
    out.reserve(points.size());
    for (const QVariant& point : points) {
        const QVariantList row = point.toList();
        if (row.size() < 2) {
            continue;
        }
        out.append(QPointF(row.at(0).toDouble(), row.at(1).toDouble()));
    }
    return out;
}

QVector<QVector<QPointF>> parsePolygons(const QVariant& polygonsVariant) {
    QVector<QVector<QPointF>> out;
    const QVariantList polygons = polygonsVariant.toList();
    out.reserve(polygons.size());
    for (const QVariant& polygon : polygons) {
        out.append(parsePointList(polygon));
    }
    return out;
}

QVector<QColor> parseColors(const QVariant& colorsVariant) {
    QVector<QColor> out;
    const QVariantList colors = colorsVariant.toList();
    out.reserve(colors.size());
    for (const QVariant& value : colors) {
        QColor color(value.toString());
        if (!color.isValid()) {
            color = QColor("#3b82f6");
        }
        out.append(color);
    }
    return out;
}

QPointF worldToScreen(
    const QPointF& world,
    double xMin,
    double yMin,
    double spanX,
    double spanY,
    double widthValue,
    double heightValue
) {
    const double sx = widthValue / spanX;
    const double sy = heightValue / spanY;
    const double x = (world.x() - xMin) * sx;
    const double y = heightValue - ((world.y() - yMin) * sy);
    return QPointF(x, y);
}

void drawAnchors(
    QPainter* painter,
    const QVector<QPointF>& polygon,
    double xMin,
    double yMin,
    double spanX,
    double spanY,
    double widthValue,
    double heightValue,
    const QColor& edgeColor
) {
    painter->setPen(QPen(edgeColor, 1.5));
    painter->setBrush(QBrush(QColor::fromRgbF(1.0, 1.0, 1.0, 0.85)));
    for (const QPointF& point : polygon) {
        const QPointF p = worldToScreen(point, xMin, yMin, spanX, spanY, widthValue, heightValue);
        painter->drawEllipse(p, 4.0, 4.0);
    }
}

}  // namespace

DiscoveryLassoOverlayItem::DiscoveryLassoOverlayItem(QQuickItem* parent)
    : QQuickPaintedItem(parent) {
    setAntialiasing(true);
}

void DiscoveryLassoOverlayItem::setLassoData(
    const QVariant& viewport,
    const QVariant& polygons,
    const QVariant& polygonColors,
    double fillOpacity,
    const QVariant& currentPolygon,
    int selectedPolygonIndex
) {
    m_hasViewport = parseViewport(viewport, m_xMin, m_xMax, m_yMin, m_yMax);
    m_polygons = parsePolygons(polygons);
    m_polygonColors = parseColors(polygonColors);
    m_fillOpacity = qBound(0.0, fillOpacity, 1.0);
    m_currentPolygon = parsePointList(currentPolygon);
    m_selectedPolygonIndex = selectedPolygonIndex;
    update();
}

void DiscoveryLassoOverlayItem::setViewport(const QVariant& viewport) {
    const bool hasViewport = parseViewport(viewport, m_xMin, m_xMax, m_yMin, m_yMax);
    if (m_hasViewport == hasViewport && !hasViewport) {
        return;
    }
    m_hasViewport = hasViewport;
    update();
}

void DiscoveryLassoOverlayItem::setDrawFill(bool drawFill) {
    if (m_drawFill == drawFill) {
        return;
    }
    m_drawFill = drawFill;
    update();
}

void DiscoveryLassoOverlayItem::paint(QPainter* painter) {
    if (!m_hasViewport) {
        return;
    }
    const double widthValue = qMax(1.0, static_cast<double>(width()));
    const double heightValue = qMax(1.0, static_cast<double>(height()));
    const double spanX = m_xMax - m_xMin;
    const double spanY = m_yMax - m_yMin;
    if (qFuzzyIsNull(spanX) || qFuzzyIsNull(spanY)) {
        return;
    }

    painter->setRenderHint(QPainter::Antialiasing, true);
    for (int polygonIndex = 0; polygonIndex < m_polygons.size(); ++polygonIndex) {
        const QVector<QPointF>& polygon = m_polygons.at(polygonIndex);
        if (polygon.size() < 3) {
            continue;
        }
        QColor stroke = polygonIndex < m_polygonColors.size()
            ? m_polygonColors.at(polygonIndex)
            : QColor("#3b82f6");
        QColor fill(stroke.lighter(125));
        fill.setAlphaF(m_fillOpacity);
        stroke.setAlpha(255);
        QPainterPath path;
        for (int i = 0; i < polygon.size(); ++i) {
            const QPointF p = worldToScreen(polygon.at(i), m_xMin, m_yMin, spanX, spanY, widthValue, heightValue);
            if (i == 0) {
                path.moveTo(p);
            } else {
                path.lineTo(p);
            }
        }
        path.closeSubpath();
        if (m_drawFill) {
            painter->fillPath(path, QBrush(fill));
        } else {
            painter->setPen(QPen(stroke, 2.0));
            painter->drawPath(path);
        }
    }

    if (!m_drawFill && m_selectedPolygonIndex >= 0 && m_selectedPolygonIndex < m_polygons.size()) {
        drawAnchors(
            painter,
            m_polygons.at(m_selectedPolygonIndex),
            m_xMin,
            m_yMin,
            spanX,
            spanY,
            widthValue,
            heightValue,
            m_selectedPolygonIndex < m_polygonColors.size()
                ? m_polygonColors.at(m_selectedPolygonIndex)
                : QColor("#3b82f6")
        );
    }

    if (!m_drawFill && !m_currentPolygon.isEmpty()) {
        painter->setPen(QPen(QColor::fromRgbF(0.9, 0.5, 0.1, 0.95), 2.0));
        QPolygonF line;
        line.reserve(m_currentPolygon.size());
        for (const QPointF& world : m_currentPolygon) {
            line.append(worldToScreen(world, m_xMin, m_yMin, spanX, spanY, widthValue, heightValue));
        }
        painter->drawPolyline(line);
        drawAnchors(
            painter,
            m_currentPolygon,
            m_xMin,
            m_yMin,
            spanX,
            spanY,
            widthValue,
            heightValue,
            QColor::fromRgbF(0.9, 0.5, 0.1, 1.0)
        );
    }
}
