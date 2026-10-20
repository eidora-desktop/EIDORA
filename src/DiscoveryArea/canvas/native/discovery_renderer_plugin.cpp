#include "discovery_render_item.h"
#include "discovery_lasso_overlay_item.h"
#include "discovery_pin_overlay_item.h"

#include <QQmlEngine>
#include <QQmlExtensionPlugin>

class DiscoveryRendererPlugin final : public QQmlExtensionPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char* uri) override {
        qmlRegisterType<DiscoveryRenderItem>(uri, 1, 0, "DiscoveryRenderItem");
        qmlRegisterType<DiscoveryLassoOverlayItem>(uri, 1, 0, "DiscoveryLassoOverlayItem");
        qmlRegisterType<DiscoveryPinOverlayItem>(uri, 1, 0, "DiscoveryPinOverlayItem");
    }
};

#include "discovery_renderer_plugin.moc"
