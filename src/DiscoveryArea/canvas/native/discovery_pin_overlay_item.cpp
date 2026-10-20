#include "discovery_pin_overlay_item.h"

#include <QColor>
#include <QFont>
#include <QFontMetrics>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QByteArray>
#include <QLineF>
#include <QSize>
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

QRectF fitRectKeepAspect(const QRectF& box, const QSize& imageSize) {
    if (imageSize.width() <= 0 || imageSize.height() <= 0 || box.width() <= 0.0 || box.height() <= 0.0) {
        return box;
    }
    const double sx = box.width() / static_cast<double>(imageSize.width());
    const double sy = box.height() / static_cast<double>(imageSize.height());
    const double scale = qMin(sx, sy);
    const double w = static_cast<double>(imageSize.width()) * scale;
    const double h = static_cast<double>(imageSize.height()) * scale;
    const double x = box.left() + (box.width() - w) * 0.5;
    const double y = box.top();
    return QRectF(x, y, w, h);
}

QSizeF fittedThumbnailSize(const QImage& image, int maxSize) {
    const double fallback = qMax(1, maxSize);
    if (image.isNull() || image.width() <= 0 || image.height() <= 0) {
        return QSizeF(fallback, fallback);
    }
    const double scale = qMin(
        fallback / static_cast<double>(image.width()),
        fallback / static_cast<double>(image.height())
    );
    return QSizeF(
        qMax(1.0, static_cast<double>(image.width()) * scale),
        qMax(1.0, static_cast<double>(image.height()) * scale)
    );
}

int styledLineWidth(
    const QString& line,
    const QFontMetrics& labelMetrics,
    const QFontMetrics& valueMetrics,
    bool showLabel,
    bool showValue
) {
    const int tabIndex = line.indexOf(QLatin1Char('\t'));
    if (tabIndex >= 0) {
        const QString label = line.left(tabIndex);
        const QString value = line.mid(tabIndex + 1);
        int width = 0;
        if (showLabel) {
            width += labelMetrics.horizontalAdvance(label);
            if (showValue && !value.isEmpty()) {
                width += labelMetrics.horizontalAdvance(QStringLiteral(" "));
            }
        }
        if (showValue) {
            width += valueMetrics.horizontalAdvance(value);
        }
        return width;
    }
    if (showValue) {
        return valueMetrics.horizontalAdvance(line);
    }
    if (showLabel) {
        return labelMetrics.horizontalAdvance(line);
    }
    return 0;
}

QPointF nearestEdgeMidpoint(const QRectF& rect, const QPointF& source) {
    const QPointF candidates[] = {
        QPointF(rect.left(), rect.center().y()),
        QPointF(rect.right(), rect.center().y()),
        QPointF(rect.center().x(), rect.top()),
        QPointF(rect.center().x(), rect.bottom()),
    };
    QPointF nearest = candidates[0];
    double nearestDistance = QLineF(source, nearest).length();
    for (int index = 1; index < 4; ++index) {
        const double distance = QLineF(source, candidates[index]).length();
        if (distance < nearestDistance) {
            nearest = candidates[index];
            nearestDistance = distance;
        }
    }
    return nearest;
}

QPointF nearestCorner(const QRectF& rect, const QPointF& source) {
    const QPointF candidates[] = {
        rect.topLeft(),
        rect.topRight(),
        rect.bottomLeft(),
        rect.bottomRight(),
    };
    QPointF nearest = candidates[0];
    double nearestDistance = QLineF(source, nearest).length();
    for (int index = 1; index < 4; ++index) {
        const double distance = QLineF(source, candidates[index]).length();
        if (distance < nearestDistance) {
            nearest = candidates[index];
            nearestDistance = distance;
        }
    }
    return nearest;
}

}  // namespace

DiscoveryPinOverlayItem::DiscoveryPinOverlayItem(QQuickItem* parent)
    : QQuickPaintedItem(parent) {
    setAntialiasing(true);
}

void DiscoveryPinOverlayItem::setPinData(
    const QVariant& viewport,
    const QVariant& pins,
    const QVariant& style,
    bool visible
) {
    m_hasViewport = parseViewport(viewport, m_xMin, m_xMax, m_yMin, m_yMax);
    m_visible = visible;
    m_pins.clear();

    const QVariantList pinList = pins.toList();
    m_pins.reserve(pinList.size());
    for (const QVariant& value : pinList) {
        const QVariantMap entry = value.toMap();
        if (!entry.value("visible", true).toBool()) {
            continue;
        }
        const QVariantList point = entry.value("point").toList();
        if (point.size() < 2) {
            continue;
        }
        const QVariantList offset = entry.value("screen_offset").toList();
        QPointF screenOffset(48.0, -36.0);
        if (offset.size() >= 2) {
            screenOffset = QPointF(offset.at(0).toDouble(), offset.at(1).toDouble());
        }
        QStringList lines;
        const QVariantList linesVariant = entry.value("lines").toList();
        for (const QVariant& line : linesVariant) {
            lines.append(line.toString());
        }
        PinEntry pin;
        pin.worldPoint = QPointF(point.at(0).toDouble(), point.at(1).toDouble());
        const QVariantList boxAnchor = entry.value("box_anchor").toList();
        if (boxAnchor.size() >= 2) {
            pin.boxWorldAnchor = QPointF(boxAnchor.at(0).toDouble(), boxAnchor.at(1).toDouble());
            pin.hasBoxWorldAnchor = true;
        }
        pin.screenOffset = screenOffset;
        pin.lines = lines;
        pin.thumbnailSize = qMax(32, entry.value("thumbnail_size", 96).toInt());
        const int thumbWidth = qMax(0, entry.value("thumbnail_width", 0).toInt());
        const int thumbHeight = qMax(0, entry.value("thumbnail_height", 0).toInt());
        const QByteArray thumbRgba = entry.value("thumbnail_rgba").toByteArray();
        if (thumbWidth > 0 && thumbHeight > 0 && thumbRgba.size() == thumbWidth * thumbHeight * 4) {
            QImage image(reinterpret_cast<const uchar*>(thumbRgba.constData()), thumbWidth, thumbHeight, thumbWidth * 4, QImage::Format_RGBA8888);
            pin.thumbnail = image.copy();
        }
        m_pins.append(pin);
    }

    const QVariantMap styleMap = style.toMap();
    if (styleMap.contains("font_size")) {
        m_fontSize = qMax(8, styleMap.value("font_size").toInt());
    }
    if (styleMap.contains("font_family")) {
        const QString family = styleMap.value("font_family").toString().trimmed();
        m_fontFamily = family.isEmpty() ? QStringLiteral("Arial") : family;
    }
    if (styleMap.contains("text_color")) {
        m_textColor = QColor(styleMap.value("text_color").toString());
    }
    if (styleMap.contains("box_color")) {
        const QColor c(styleMap.value("box_color").toString());
        if (c.isValid()) {
            m_boxColor = c;
        }
    }
    if (styleMap.contains("border_color")) {
        const QColor c(styleMap.value("border_color").toString());
        if (c.isValid()) {
            m_borderColor = c;
        }
    }
    if (styleMap.contains("connector_color")) {
        const QColor c(styleMap.value("connector_color").toString());
        if (c.isValid()) {
            m_connectorColor = c;
        }
    }
    if (styleMap.contains("connector_width")) {
        m_connectorWidth = qMax(0.5, styleMap.value("connector_width").toDouble());
    }
    const bool boxVisible = styleMap.value("box_visible", true).toBool();
    const bool connectorVisible = styleMap.value("connector_visible", true).toBool();
    const bool textVisible = styleMap.value("text_visible", true).toBool();
    const double boxOpacity = qBound(0.0, styleMap.value("box_opacity", 1.0).toDouble(), 1.0);
    const double borderWidth = qMax(0.0, styleMap.value("border_width", 1.0).toDouble());
    m_boxColor.setAlphaF(boxOpacity);
    m_borderColor.setAlphaF(boxOpacity);
    m_drawBox = boxVisible;
    m_drawConnector = connectorVisible;
    m_drawText = textVisible;
    m_borderWidth = borderWidth;
    m_showLabel = styleMap.value("show_label", true).toBool();
    m_showValue = styleMap.value("show_value", true).toBool();
    m_labelBold = styleMap.value("text_label_bold", false).toBool();
    m_labelItalic = styleMap.value("text_label_italic", false).toBool();
    m_labelUnderline = styleMap.value("text_label_underline", false).toBool();
    m_valueBold = styleMap.value("text_value_bold", false).toBool();
    m_valueItalic = styleMap.value("text_value_italic", false).toBool();
    m_valueUnderline = styleMap.value("text_value_underline", false).toBool();
    m_drawThumbnail = styleMap.value("thumbnail_visible", false).toBool();
    m_thumbnailPosition = styleMap.value("thumbnail_position", "left").toString();
    m_connectorTarget = styleMap.value("connector_target", "center").toString().trimmed().toLower();
    if (m_connectorTarget == QStringLiteral("box")) {
        m_connectorTarget = QStringLiteral("center");
    } else if (m_connectorTarget == QStringLiteral("thumbnail")) {
        m_connectorTarget = QStringLiteral("edge");
    }
    if (m_connectorTarget != QStringLiteral("center")
        && m_connectorTarget != QStringLiteral("edge")
        && m_connectorTarget != QStringLiteral("corner")) {
        m_connectorTarget = QStringLiteral("center");
    }

    update();
}

void DiscoveryPinOverlayItem::setViewport(const QVariant& viewport) {
    const bool hasViewport = parseViewport(viewport, m_xMin, m_xMax, m_yMin, m_yMax);
    if (m_hasViewport == hasViewport && !hasViewport) {
        return;
    }
    m_hasViewport = hasViewport;
    update();
}

void DiscoveryPinOverlayItem::paint(QPainter* painter) {
    if (!m_visible || !m_hasViewport || m_pins.isEmpty()) {
        return;
    }
    const double widthValue = qMax(1.0, static_cast<double>(width()));
    const double heightValue = qMax(1.0, static_cast<double>(height()));
    const double spanX = m_xMax - m_xMin;
    const double spanY = m_yMax - m_yMin;
    if (qFuzzyIsNull(spanX) || qFuzzyIsNull(spanY)) {
        return;
    }

    QFont font = painter->font();
    if (!m_fontFamily.isEmpty()) {
        font.setFamily(m_fontFamily);
    }
    font.setPointSize(m_fontSize);
    QFont labelFont(font);
    labelFont.setBold(m_labelBold);
    labelFont.setItalic(m_labelItalic);
    labelFont.setUnderline(m_labelUnderline);
    QFont valueFont(font);
    valueFont.setBold(m_valueBold);
    valueFont.setItalic(m_valueItalic);
    valueFont.setUnderline(m_valueUnderline);
    const QFontMetrics labelMetrics(labelFont);
    const QFontMetrics valueMetrics(valueFont);
    const int lineHeight = qMax(labelMetrics.height(), valueMetrics.height());
    const int padding = 6;
    const int contentGap = 6;

    if (m_drawConnector) {
        painter->setPen(QPen(m_connectorColor, m_connectorWidth));
    }
    for (const PinEntry& pin : m_pins) {
        if (!m_drawConnector) {
            break;
        }
        const QPointF anchor = worldToScreen(pin.worldPoint, m_xMin, m_yMin, spanX, spanY, widthValue, heightValue);
        int textWidth = 0;
        for (const QString& line : pin.lines) {
            textWidth = qMax(
                textWidth,
                styledLineWidth(line, labelMetrics, valueMetrics, m_showLabel, m_showValue)
            );
        }
        const int textHeight = qMax(0, lineHeight * pin.lines.size());
        const bool hasText = textWidth > 0 || textHeight > 0;
        const bool drawThumb = m_drawThumbnail;
        const int thumbSize = drawThumb ? qMax(32, pin.thumbnailSize) : 0;
        const QSizeF thumbDisplaySize = drawThumb ? fittedThumbnailSize(pin.thumbnail, thumbSize) : QSizeF(0.0, 0.0);
        const double thumbWidth = thumbDisplaySize.width();
        const double thumbHeight = thumbDisplaySize.height();
        const bool horizontalLayout =
            m_thumbnailPosition.compare("left", Qt::CaseInsensitive) == 0
            || m_thumbnailPosition.compare("right", Qt::CaseInsensitive) == 0;
        const double contentWidth =
            drawThumb
            ? (
                horizontalLayout
                ? (thumbWidth + (hasText ? contentGap : 0) + textWidth)
                : qMax(thumbWidth, static_cast<double>(textWidth))
            )
            : textWidth;
        const double contentHeight =
            drawThumb
            ? (
                horizontalLayout
                ? qMax(thumbHeight, static_cast<double>(textHeight))
                : (thumbHeight + (hasText ? contentGap : 0) + textHeight)
            )
            : textHeight;
        const QPointF topLeft = pin.hasBoxWorldAnchor
            ? worldToScreen(pin.boxWorldAnchor, m_xMin, m_yMin, spanX, spanY, widthValue, heightValue)
            : QPointF(anchor.x() + pin.screenOffset.x(), anchor.y() + pin.screenOffset.y());
        QRectF boxRect(
            topLeft.x(),
            topLeft.y(),
            qMax(1.0, contentWidth + padding * 2.0),
            qMax(1.0, contentHeight + padding * 2.0)
        );
        QRectF thumbnailRect(0.0, 0.0, 0.0, 0.0);
        if (drawThumb) {
            if (m_thumbnailPosition.compare("right", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(boxRect.right() - padding - thumbWidth, boxRect.top() + padding, thumbWidth, thumbHeight);
            } else if (m_thumbnailPosition.compare("top", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(boxRect.left() + padding + qMax(0.0, (contentWidth - thumbWidth) / 2.0), boxRect.top() + padding, thumbWidth, thumbHeight);
            } else if (m_thumbnailPosition.compare("bottom", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(boxRect.left() + padding + qMax(0.0, (contentWidth - thumbWidth) / 2.0), boxRect.bottom() - padding - thumbHeight, thumbWidth, thumbHeight);
            } else {
                thumbnailRect = QRectF(boxRect.left() + padding, boxRect.top() + padding, thumbWidth, thumbHeight);
            }
        }
        const QRectF targetRect = (!m_drawBox && drawThumb) ? thumbnailRect : boxRect;
        if (m_connectorTarget == QStringLiteral("corner")) {
            painter->drawLine(anchor, nearestCorner(targetRect, anchor));
        } else if (m_connectorTarget == QStringLiteral("edge")) {
            painter->drawLine(anchor, nearestEdgeMidpoint(targetRect, anchor));
        } else {
            QPainterPath clip;
            clip.addRect(QRectF(0.0, 0.0, widthValue, heightValue));
            if (m_drawBox) {
                clip.addRoundedRect(targetRect, 4.0, 4.0);
            } else {
                clip.addRect(targetRect);
            }
            clip.setFillRule(Qt::OddEvenFill);
            painter->save();
            painter->setClipPath(clip, Qt::ReplaceClip);
            painter->drawLine(anchor, targetRect.center());
            painter->restore();
        }
    }

    for (const PinEntry& pin : m_pins) {
        const QPointF anchor = worldToScreen(pin.worldPoint, m_xMin, m_yMin, spanX, spanY, widthValue, heightValue);
        int textWidth = 0;
        for (const QString& line : pin.lines) {
            textWidth = qMax(
                textWidth,
                styledLineWidth(line, labelMetrics, valueMetrics, m_showLabel, m_showValue)
            );
        }
        const int textHeight = qMax(0, lineHeight * pin.lines.size());
        const bool hasText = textWidth > 0 || textHeight > 0;
        const bool drawThumb = m_drawThumbnail;
        const int thumbSize = drawThumb ? qMax(32, pin.thumbnailSize) : 0;
        const QSizeF thumbDisplaySize = drawThumb ? fittedThumbnailSize(pin.thumbnail, thumbSize) : QSizeF(0.0, 0.0);
        const double thumbWidth = thumbDisplaySize.width();
        const double thumbHeight = thumbDisplaySize.height();
        const bool horizontalLayout =
            m_thumbnailPosition.compare("left", Qt::CaseInsensitive) == 0
            || m_thumbnailPosition.compare("right", Qt::CaseInsensitive) == 0;
        const double contentWidth =
            drawThumb
            ? (horizontalLayout ? (thumbWidth + (hasText ? contentGap : 0) + textWidth) : qMax(thumbWidth, static_cast<double>(textWidth)))
            : textWidth;
        const double contentHeight =
            drawThumb
            ? (horizontalLayout ? qMax(thumbHeight, static_cast<double>(textHeight)) : (thumbHeight + (hasText ? contentGap : 0) + textHeight))
            : textHeight;
        const double boxWidth = qMax(1.0, contentWidth + padding * 2.0);
        const double boxHeight = qMax(1.0, contentHeight + padding * 2.0);
        const QPointF topLeft = pin.hasBoxWorldAnchor
            ? worldToScreen(pin.boxWorldAnchor, m_xMin, m_yMin, spanX, spanY, widthValue, heightValue)
            : QPointF(anchor.x() + pin.screenOffset.x(), anchor.y() + pin.screenOffset.y());
        QRectF boxRect(topLeft.x(), topLeft.y(), boxWidth, boxHeight);
        QRectF thumbnailRect(0.0, 0.0, 0.0, 0.0);
        qreal textLeft = boxRect.left() + padding;
        qreal textTop = boxRect.top() + padding;
        if (drawThumb) {
            if (m_thumbnailPosition.compare("right", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(
                    boxRect.right() - padding - thumbWidth,
                    boxRect.top() + padding,
                    thumbWidth,
                    thumbHeight
                );
            } else if (m_thumbnailPosition.compare("top", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(
                    boxRect.left() + padding + qMax(0.0, (contentWidth - thumbWidth) / 2.0),
                    boxRect.top() + padding,
                    thumbWidth,
                    thumbHeight
                );
                textTop = thumbnailRect.bottom() + contentGap;
                textLeft = boxRect.left() + padding;
            } else if (m_thumbnailPosition.compare("bottom", Qt::CaseInsensitive) == 0) {
                thumbnailRect = QRectF(
                    boxRect.left() + padding + qMax(0.0, (contentWidth - thumbWidth) / 2.0),
                    boxRect.bottom() - padding - thumbHeight,
                    thumbWidth,
                    thumbHeight
                );
                textTop = boxRect.top() + padding;
                textLeft = boxRect.left() + padding;
            } else {
                thumbnailRect = QRectF(
                    boxRect.left() + padding,
                    boxRect.top() + padding,
                    thumbWidth,
                    thumbHeight
                );
                textLeft = thumbnailRect.right() + contentGap;
            }
            if (horizontalLayout) {
                if (m_thumbnailPosition.compare("right", Qt::CaseInsensitive) == 0) {
                    textLeft = boxRect.left() + padding;
                }
            }
        }

        if (m_drawBox) {
            if (m_borderWidth <= 0.0) {
                painter->setPen(Qt::NoPen);
            } else {
                painter->setPen(QPen(m_borderColor, m_borderWidth));
            }
            painter->setBrush(m_boxColor);
            painter->drawRoundedRect(boxRect, 4.0, 4.0);
        }

        if (m_drawText) {
            painter->setPen(QPen(m_textColor, 1.0));
            painter->setBrush(Qt::NoBrush);
            qreal y = textTop + lineHeight;
            for (const QString& line : pin.lines) {
                qreal x = textLeft;
                const int tabIndex = line.indexOf(QLatin1Char('\t'));
                if (tabIndex >= 0) {
                    const QString label = line.left(tabIndex);
                    const QString value = line.mid(tabIndex + 1);
                    if (m_showLabel) {
                        painter->setFont(labelFont);
                        painter->drawText(QPointF(x, y), label);
                        x += labelMetrics.horizontalAdvance(label);
                        if (m_showValue && !value.isEmpty()) {
                            x += labelMetrics.horizontalAdvance(QStringLiteral(" "));
                        }
                    }
                    if (m_showValue) {
                        painter->setFont(valueFont);
                        painter->drawText(QPointF(x, y), value);
                    }
                } else if (m_showValue) {
                    painter->setFont(valueFont);
                    painter->drawText(QPointF(x, y), line);
                } else if (m_showLabel) {
                    painter->setFont(labelFont);
                    painter->drawText(QPointF(x, y), line);
                }
                y += lineHeight;
            }
        }

        if (drawThumb) {
            if (!pin.thumbnail.isNull()) {
                painter->setPen(Qt::NoPen);
                const QRectF imageRect = fitRectKeepAspect(thumbnailRect, pin.thumbnail.size());
                painter->drawImage(imageRect, pin.thumbnail);
            } else {
                painter->setPen(Qt::NoPen);
                painter->setBrush(QBrush(QColor::fromRgbF(0.88, 0.88, 0.88, 1.0)));
                painter->drawRect(thumbnailRect);
            }
        }
    }
}
