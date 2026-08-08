import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    property alias cfg_refreshSeconds: refresh.value
    property alias cfg_showProvider: provider.checked
    property alias cfg_showResetTime: reset.checked
    property alias cfg_compactMetric: metric.currentValue

    Controls.SpinBox {
        id: refresh
        Kirigami.FormData.label: i18n("Anzeige aktualisieren:")
        from: 10
        to: 600
        stepSize: 10
        textFromValue: value => i18n("%1 Sekunden", value)
        valueFromText: text => parseInt(text)
    }
    Controls.CheckBox {
        id: provider
        Kirigami.FormData.label: i18n("Beschriftung:")
        text: i18n("Anbieter anzeigen")
    }
    Controls.CheckBox {
        id: reset
        text: i18n("Reset-Countdown anzeigen")
    }
    Controls.ComboBox {
        id: metric
        Kirigami.FormData.label: i18n("Panelwert:")
        textRole: "text"
        valueRole: "value"
        model: [
            {text: i18n("Höchste Auslastung"), value: "maximum"},
            {text: i18n("Aktive Session"), value: "session"},
            {text: i18n("Wochenlimit"), value: "weekly"}
        ]
    }
    Controls.Label {
        Kirigami.FormData.isSection: true
        text: i18n("Konten, Aliase, Zugangsdaten und Alarm-Schwellen werden in ~/.config/token-limits/config.json verwaltet.")
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }
}
