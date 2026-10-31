// DSM 桌面应用窗口注册（浮窗机制）。
// ui/config 里以本文件名为节键声明两个 Ext 类；INFO 的 dsmappname 必须等于
// AppInstance 类名。窗口内容为 iframe，加载 DSM 托管的套件页面（同源）。
Ext.ns('MeowPerth.AutoWallpaper');

Ext.define('MeowPerth.AutoWallpaper.AppInstance', {
    extend: 'SYNO.SDS.AppInstance',
    appWindowName: 'MeowPerth.AutoWallpaper.AppWindow',
    constructor: function () {
        this.callParent(arguments);
    },
});

Ext.define('MeowPerth.AutoWallpaper.AppWindow', {
    extend: 'SYNO.SDS.AppWindow',
    constructor: function (config) {
        const cfg = Ext.apply(
            {
                resizable: true,
                maximizable: true,
                minimizable: true,
                width: 1120,
                height: 760,
                minWidth: 720,
                minHeight: 520,
                layout: 'fit',
                border: false,
                cls: 'syno-app-autowallpaper-wrapper',
                items: [
                    {
                        xtype: 'box',
                        autoEl: {
                            tag: 'iframe',
                            src: '/webman/3rdparty/autowallpaper/index.html',
                            frameborder: '0',
                            style: 'width:100%; height:100%; border:none;',
                        },
                    },
                ],
            },
            config
        );
        this.callParent([cfg]);
    },
    onOpen: function (info) {
        this.callParent([info]);
    },
    onRequest: function (info) {
        this.onOpen(info);
    },
});
