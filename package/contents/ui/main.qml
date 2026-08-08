import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import QtCore
import org.kde.kirigami as Kirigami
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasmoid
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
    id: root

    property var accounts: []
    property string loadError: ""
    property int generatedAt: 0
    property string homePath: decodeURIComponent(StandardPaths.writableLocation(StandardPaths.HomeLocation).toString().replace(/^file:\/\//, ""))
    property string statusCommand: homePath + "/.local/bin/token-limits-collector --print-status"

    Plasmoid.title: i18n("Token Limits")
    toolTipMainText: i18n("Token Limits")
    toolTipSubText: accounts.length ? i18n("%1 Konten · Maximum %2%", accounts.length, Math.round(maximumPercent())) : (loadError || i18n("Noch keine Daten"))

    function applyStatus(text) {
        try {
            const parsed = JSON.parse(text)
            root.accounts = parsed.accounts || []
            root.generatedAt = parsed.generated_at || 0
            root.loadError = parsed.error || ""
        } catch (error) {
            root.loadError = i18n("Statusausgabe ist ungültig")
        }
    }

    function reloadStatus() {
        statusSource.disconnectSource(statusCommand)
        statusSource.connectSource(statusCommand)
    }

    function maximumPercent(metric) {
        let maximum = 0
        for (const account of accounts) {
            for (const key of Object.keys(account.windows || {})) {
                if (metric && metric !== "maximum" && key !== metric) continue
                maximum = Math.max(maximum, Number(account.windows[key].used_percent || 0))
            }
        }
        return maximum
    }

    function formatDuration(seconds) {
        if (seconds === null || seconds === undefined) return i18n("Reset unbekannt")
        const value = Math.max(0, Number(seconds) - Math.max(0, Math.floor(Date.now() / 1000) - generatedAt))
        const days = Math.floor(value / 86400)
        const hours = Math.floor((value % 86400) / 3600)
        const minutes = Math.floor((value % 3600) / 60)
        if (days > 0) return i18n("Reset in %1 T %2 Std", days, hours)
        if (hours > 0) return i18n("Reset in %1 Std %2 Min", hours, minutes)
        return i18n("Reset in %1 Min", minutes)
    }

    function windowRows(account) {
        const rows = []
        if (account.windows && account.windows.session) rows.push({name: i18n("Session"), key: "session", data: account.windows.session})
        if (account.windows && account.windows.weekly) rows.push({name: i18n("Woche"), key: "weekly", data: account.windows.weekly})
        return rows
    }

    function quotaColor(percent) {
        if (percent >= 95) return Kirigami.Theme.negativeTextColor
        if (percent >= 80) return Kirigami.Theme.neutralTextColor
        return Kirigami.Theme.positiveTextColor
    }

    Plasma5Support.DataSource {
        id: statusSource
        engine: "executable"
        interval: Math.max(10, Plasmoid.configuration.refreshSeconds) * 1000
        connectedSources: [root.statusCommand]
        onNewData: function(sourceName, data) {
            if (data["exit code"] === 0 && data.stdout) root.applyStatus(data.stdout)
            else root.loadError = data.stderr || i18n("Collector noch nicht bereit")
        }
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Jetzt neu laden")
            icon.name: "view-refresh"
            onTriggered: root.reloadStatus()
        }
    ]

    compactRepresentation: MouseArea {
        id: compact
        implicitWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        implicitHeight: Math.max(Kirigami.Units.iconSizes.smallMedium, row.implicitHeight)
        onClicked: root.expanded = !root.expanded

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: Kirigami.Units.smallSpacing
            Kirigami.Icon {
                source: "view-statistics"
                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                implicitHeight: implicitWidth
                color: root.quotaColor(root.maximumPercent(Plasmoid.configuration.compactMetric))
            }
            Controls.Label {
                text: Math.round(root.maximumPercent(Plasmoid.configuration.compactMetric)) + "%"
                font.bold: true
            }
        }
    }

    fullRepresentation: Item {
        implicitWidth: Kirigami.Units.gridUnit * 25
        implicitHeight: Kirigami.Units.gridUnit * 30

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: Kirigami.Units.largeSpacing

            RowLayout {
                Layout.fillWidth: true
                Kirigami.Heading { text: i18n("Token Limits"); level: 2; Layout.fillWidth: true }
                Controls.ToolButton { icon.name: "view-refresh"; onClicked: root.reloadStatus(); Controls.ToolTip.text: i18n("Neu laden"); Controls.ToolTip.visible: hovered }
            }

            Controls.Label {
                visible: root.loadError !== ""
                text: root.loadError + "\n" + i18n("Prüfe token-limits.service und ~/.config/token-limits/config.json")
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.negativeTextColor
                Layout.fillWidth: true
            }

            ListView {
                id: accountList
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Kirigami.Units.largeSpacing
                clip: true
                model: root.accounts
                delegate: Kirigami.AbstractCard {
                    required property var modelData
                    width: accountList.width
                    contentItem: ColumnLayout {
                        spacing: Kirigami.Units.smallSpacing
                        RowLayout {
                            Layout.fillWidth: true
                            Kirigami.Icon {
                                source: modelData.available ? "emblem-success" : "emblem-error"
                                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                                implicitHeight: implicitWidth
                            }
                            Kirigami.Heading {
                                text: (Plasmoid.configuration.showProvider ? modelData.provider.charAt(0).toUpperCase() + modelData.provider.slice(1) + " · " : "") + modelData.alias
                                level: 3
                                Layout.fillWidth: true
                            }
                            Controls.Label {
                                text: modelData.available ? i18n("verfügbar") : i18n("limitiert")
                                color: modelData.available ? Kirigami.Theme.positiveTextColor : Kirigami.Theme.negativeTextColor
                            }
                        }
                        Controls.Label {
                            visible: !!modelData.error
                            text: modelData.error || ""
                            color: Kirigami.Theme.negativeTextColor
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                        Repeater {
                            model: root.windowRows(modelData)
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 2
                                RowLayout {
                                    Layout.fillWidth: true
                                    Controls.Label { text: modelData.name; Layout.fillWidth: true }
                                    Controls.Label { text: Number(modelData.data.used_percent).toFixed(0) + "%"; font.bold: true; color: root.quotaColor(modelData.data.used_percent) }
                                }
                                Controls.ProgressBar {
                                    Layout.fillWidth: true
                                    from: 0; to: 100; value: modelData.data.used_percent
                                }
                                Controls.Label {
                                    visible: Plasmoid.configuration.showResetTime
                                    text: root.formatDuration(modelData.data.reset_in_seconds)
                                    opacity: 0.7
                                    font.pointSize: Kirigami.Theme.smallFont.pointSize
                                }
                            }
                        }
                    }
                }
            }

            Controls.Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignRight
                opacity: 0.6
                text: generatedAt ? i18n("Stand: %1", new Date(generatedAt * 1000).toLocaleTimeString()) : ""
            }
        }
    }
}
