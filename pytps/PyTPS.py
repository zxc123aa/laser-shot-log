import sys


import ui_PyTPS
from PySide6 import QtWidgets, QtGui, QtCore
import TSettings, CustomColormaps, TDraw, TPSParabola, TWatchDog, TSolve, TSave, TCompare
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
import matplotlib.pyplot as plt
import os
import json
from watchdog.observers import Observer
import datetime
import threading
import urllib.request


# ============ 打靶日志系统上报配置 ============
# 截止能量确认后自动上报：优先发 8767 填报页（按文件名精确匹配发次行，
# 8767 页面同步显示），helper 不可达时直连 A 机（按图片 mtime 匹配时间窗）。
LOG_HELPER_URL = "http://127.0.0.1:8767"
LOG_SERVER_URL = "http://10.0.23.155:8765"
LOG_ENERGY_FIELD = "tps_h"
LOG_MATCH_WINDOW = 15


class _ReportSignal(QtCore.QObject):
    """后台线程上报结果 → 主线程状态栏提示"""
    done = QtCore.Signal(str)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        # 初始化UI
        super().__init__()

        # self.pv = PV("TPS_pv:TPS_cutoff_energy:pv")
        self.ui = ui_PyTPS.Ui_MainWindow()
        self.ui.setupUi(self)
        self.icon = QtGui.QIcon()
        self.icon.addPixmap(QtGui.QPixmap("icon.png"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.setWindowIcon(self.icon)

        # 初始化参数，self中新增了TPS,imagePath,imageFileName,savePath,MCPPath,X0,Y0,dY,Emin,Emax,colormin,colormax,specEmin,specEmax,specdNdEmin,specdNdEmax,specWindow变量
        TSettings.TPSinit(self)

        # 初始化新滤波参数（如果配置文件中没有，使用默认值）
        if not hasattr(self, 'thresholdValue'):
            self.thresholdValue = 500
        if not hasattr(self, 'minConnectedArea'):
            self.minConnectedArea = 50
        if not hasattr(self, 'fadeRadius'):
            self.fadeRadius = 20

        self.mode = "offline"
        self.zoomed = False
        self.isShowParabola = False
        self.isShowParticleLabel = False
        self.enabledParticleLabels = set()
        self.isAutoSave = False
        self.isAnalysing = False
        self.is8bitImage = False
        self.imageData = []
        self.rawImage = []
        self.analysisImage = []
        self.displayImage = []
        self.specData = []
        self.specDataRaw = []
        self.noiseSpec = []
        self.TPSParabola = []
        self.analysingFileList = []
        self.analysingDataList = []
        self.cutoffEnergyList = []
        self.spectrumSmoothWindow = 31
        self.cutoffSnrThreshold = 3.0
        self.cutoffConsecutiveBins = 3
        self.adaptiveMinCounts = 100
        self.weightedSmoothFactor = 1.5
        self.isManualCursor = False
        self.isDrawingMask = False
        self.manualMaskPoints = []
        self.manualMaskPolygons = []
        self.manualMask = None
        self.maskClickId = None
        self.showManualMaskOverlay = False
        self.maskBgSigma = 3
        self.maskBgPercentile = 35
        self.backgroundMode = "offset"
        self.backgroundOffset = -18
        self.backgroundWidth = 6
        self.analysisPreprocess = "hot_pixel"
        self.analysisMedianSize = 3
        self.spectrumScale = "log"
        self.cutoffMemoryFile = os.path.join(".", "cutoff_memory.json")
        self.cutoffMemory = self.loadCutoffMemory()
        self._reportSignal = _ReportSignal()
        self._reportSignal.done.connect(self.showReportStatus)
        self.useDefaultSavePath = True

        # 初始化文件列表
        TSettings.getList(self)
        # 初始化离子种类列表和伪彩图类型列表，此处创建了self中的A,Z,cmap变量
        TSettings.particalList(self)
        TSettings.colormapList(self)

        # 初始化参数写入GUI
        self.ui.lineEdit_imagePath.setText(self.imagePath)
        self.ensureSavePath()
        self.ui.lineEdit_savePath.setText(self.savePath)
        self.ui.lineEdit_MCPPath.setText(self.MCPPath)
        self.ui.lineEdit_X0.setText(np.str_(self.X0))
        self.ui.lineEdit_Y0.setText(np.str_(self.Y0))
        self.ui.lineEdit_dY.setText(np.str_(self.dY))
        self.ui.lineEdit_Emin.setText(np.str_(self.Emin))
        self.ui.lineEdit_Emax.setText(np.str_(self.Emax))
        self.ui.lineEdit_colormin.setText(np.str_(self.colormin))
        self.ui.lineEdit_colormax.setText(np.str_(self.colormax))
        self.ui.lineEdit_specEmin.setText(np.str_(self.specEmin))
        self.ui.lineEdit_specEmax.setText(np.str_(self.specEmax))
        self.ui.lineEdit_specdNdEmin.setText(np.format_float_scientific(self.specdNdEmin))
        self.ui.lineEdit_specdNdEmax.setText(np.format_float_scientific(self.specdNdEmax))
        self.ui.lineEdit_specWindow.setText(np.str_(self.specWindow))

        # 设置文本框验证规则
        self.ui.lineEdit_A.setValidator(QtGui.QIntValidator(self.Z, 500))
        self.ui.lineEdit_Z.setValidator(QtGui.QIntValidator(0, self.A))
        self.ui.lineEdit_X0.setValidator(QtGui.QIntValidator(0, 1024))
        self.ui.lineEdit_Y0.setValidator(QtGui.QIntValidator(0, 1024))
        self.ui.lineEdit_dY.setValidator(QtGui.QIntValidator(0, 20))
        self.ui.lineEdit_Emin.setValidator(QtGui.QDoubleValidator(0.0, self.Emax, 5))
        self.ui.lineEdit_Emax.setValidator(QtGui.QDoubleValidator(self.Emin, 1000.0, 5))
        self.ui.lineEdit_colormin.setValidator(QtGui.QIntValidator(0, self.colormax))
        self.ui.lineEdit_colormax.setValidator(QtGui.QIntValidator(self.colormin, 65535))
        self.ui.lineEdit_specEmin.setValidator(QtGui.QDoubleValidator(0.0, self.specEmax, 5))
        self.ui.lineEdit_specEmax.setValidator(QtGui.QDoubleValidator(self.specEmin, 1000.0, 5))
        self.ui.lineEdit_specdNdEmin.setValidator(QtGui.QDoubleValidator(0.0, self.specdNdEmax, 5))
        self.ui.lineEdit_specdNdEmax.setValidator(QtGui.QDoubleValidator(self.specdNdEmin, 1e100, 5))
        self.ui.lineEdit_specWindow.setValidator(QtGui.QDoubleValidator(0.0, self.Emax, 3))

        # 初始化图片画布
        self.imageFig = plt.figure()
        self.imageCanvas = FigureCanvas(self.imageFig)
        self.ui.gridLayout_image.addWidget(self.imageCanvas)

        # 初始化能谱画布
        self.specFig = plt.figure()
        self.specCanvas = FigureCanvas(self.specFig)
        self.ui.verticalLayout_spec.addWidget(self.specCanvas)

        # 动态创建滤波UI（新增）
        self.createFilterUI()

        # 创建对比窗口（新增）
        self.compareWindow = None

        # 信号绑定

        # 各种路径浏览
        self.ui.pushButton_browseImagePath.clicked.connect(self.browseImagePath)
        self.ui.pushButton_browseMCPPath.clicked.connect(self.browseMCPPath)
        self.ui.pushButton_browseSavePath.clicked.connect(self.browseSavePath)
        self.ui.pushButton_refreshImagePath.clicked.connect(self.refreshImagePath)
        self.ui.pushButton_openTPS.clicked.connect(self.openTPS)

        # 各种数值改变
        self.ui.pushButton_refreshTPS.clicked.connect(self.refreshTPS)
        self.ui.lineEdit_A.textChanged.connect(self.setA)
        self.ui.lineEdit_Z.textChanged.connect(self.setZ)
        self.ui.comboBox_particals.currentIndexChanged.connect(self.choosePartical)
        self.ui.comboBox_colormap.currentTextChanged.connect(self.chooseColormap)
        self.ui.lineEdit_X0.editingFinished.connect(self.setX0)
        self.ui.lineEdit_Y0.editingFinished.connect(self.setY0)
        self.ui.lineEdit_dY.editingFinished.connect(self.setdY)
        self.ui.lineEdit_Emin.editingFinished.connect(self.setEmin)
        self.ui.lineEdit_Emax.editingFinished.connect(self.setEmax)
        self.ui.lineEdit_colormin.editingFinished.connect(self.setColormin)
        self.ui.lineEdit_colormax.editingFinished.connect(self.setColormax)
        self.ui.lineEdit_specEmin.editingFinished.connect(self.setSpecEmin)
        self.ui.lineEdit_specEmax.editingFinished.connect(self.setSpecEmax)
        self.ui.lineEdit_specdNdEmin.editingFinished.connect(self.setSpecdNdEmin)
        self.ui.lineEdit_specdNdEmax.editingFinished.connect(self.setSpecdNdEmax)
        self.ui.lineEdit_specWindow.editingFinished.connect(self.setSpecWindow)

        # 离线模式：选择图片绘图解谱
        self.ui.listWidget.itemClicked.connect(self.selectImage)

        # 切换在线/离线/分析模式
        self.ui.radioButton_online.toggled.connect(self.modeSwitch)
        self.ui.radioButton_offline.toggled.connect(self.modeSwitch)
        self.ui.checkBox_analyse.stateChanged.connect(self.analyseMode)

        # 是否显示参考抛物线
        self.ui.checkBox_parabola.stateChanged.connect(self.showParabola)
        self.createParticleLabelCheckbox()

        # 保存当前结果
        self.ui.pushButton_saveSpec.clicked.connect(self.saveSpec)
        self.ui.pushButton_saveImage.clicked.connect(self.saveImage)

        # 是否自动保存
        self.ui.checkBox_autoSave.stateChanged.connect(self.autoSave)

        # 图片缩放
        self.ui.pushButton_zoom.toggled.connect(self.startZoom)
        self.ui.pushButton_origin.clicked.connect(self.stopZoom)
        self.imageFig.canvas.mpl_connect('scroll_event', self.wheelZoom)

        # 点击能谱曲线移动光标
        self.specFig.canvas.mpl_connect('button_press_event', self.setSpecCursor)

        # 帮助&关于
        self.ui.pushButton_help.clicked.connect(self.help)
        self.ui.pushButton_about.clicked.connect(self.about)

        # 添加对比窗口按钮（新增）
        self.createCutoffMemoryButton()
        self.createCompareButton()

    # 各种槽函数

    def browseImagePath(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹", ".")
        if os.path.exists(path) == False:
            QtWidgets.QMessageBox.critical(self, "错误", "路径不存在，请重新选择！")
            return
        self.imagePath = path
        self.ui.lineEdit_imagePath.setText(self.imagePath)
        if self.useDefaultSavePath:
            self.ensureSavePath()
        if self.mode == "online":
            observer.unschedule_all()
            observer.schedule(event_handler, self.imagePath, recursive=False)

    def browseMCPPath(self):
        path = QtWidgets.QFileDialog.getOpenFileName(self, "选择MCP效率文件", ".")
        if os.path.exists(path) == False:
            QtWidgets.QMessageBox.critical(self, "错误", "路径不存在，请重新选择！")
            return
        self.MCPPath, _ = path
        self.ui.lineEdit_MCPPath.setText(self.MCPPath)
        TSolve.Solve(self)

    def browseSavePath(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择保存文件夹", ".")
        if os.path.exists(path) == False:
            QtWidgets.QMessageBox.critical(self, "错误", "路径不存在，请重新选择！")
            return
        self.savePath = path
        self.useDefaultSavePath = False
        self.ui.lineEdit_savePath.setText(self.savePath)

    def ensureSavePath(self):
        if getattr(self, "useDefaultSavePath", True):
            image_path = os.fspath(self.imagePath) if self.imagePath else "."
            path = os.path.join(image_path, "能谱处理")
        else:
            path = os.fspath(self.savePath) if self.savePath else "."
        try:
            os.makedirs(path, exist_ok=True)
            self.savePath = path
            if hasattr(self, "ui"):
                self.ui.lineEdit_savePath.setText(self.savePath)
            return path
        except OSError:
            fallback = os.path.join(".", "output", "能谱处理")
            os.makedirs(fallback, exist_ok=True)
            print(f"[PyTPS] 保存路径不可用：{path}，已切换到：{fallback}")
            self.savePath = fallback
            if hasattr(self, "ui"):
                self.ui.lineEdit_savePath.setText(self.savePath)
            return fallback

    def refreshImagePath(self):
        self.imagePath = self.ui.lineEdit_imagePath.text()
        self.ui.listWidget.clear()
        TSettings.getList(self)
        self.ui.lineEdit_imagePath.setText(self.imagePath)
        if self.useDefaultSavePath:
            self.ensureSavePath()

    def openTPS(self):
        os.startfile("TPS_Settings.ini")

    def refreshTPS(self):
        self.TPS = TSettings.TPS()
        TSolve.Solve(self)

    def setA(self):
        self.A = int(self.ui.lineEdit_A.text())

    def setZ(self):
        self.Z = int(self.ui.lineEdit_Z.text())

    def choosePartical(self, index):
        if index == 12:
            self.ui.lineEdit_A.setReadOnly(False)
            self.ui.lineEdit_Z.setReadOnly(False)
        else:
            self.ui.lineEdit_A.setReadOnly(True)
            self.ui.lineEdit_Z.setReadOnly(True)
            if index == 0:
                self.ui.lineEdit_A.setText("1")
                self.ui.lineEdit_Z.setText("1")
            elif index == 1:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("6")
            elif index == 2:
                self.ui.lineEdit_A.setText("16")
                self.ui.lineEdit_Z.setText("8")
            elif index == 3:
                self.ui.lineEdit_A.setText("16")
                self.ui.lineEdit_Z.setText("7")
            elif index == 4:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("5")
            elif index == 5:
                self.ui.lineEdit_A.setText("16")
                self.ui.lineEdit_Z.setText("6")
            elif index == 6:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("4")
            elif index == 7:
                self.ui.lineEdit_A.setText("16")
                self.ui.lineEdit_Z.setText("5")
            elif index == 8:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("3")
            elif index == 9:
                self.ui.lineEdit_A.setText("16")
                self.ui.lineEdit_Z.setText("4")
            elif index == 10:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("2")
            elif index == 11:
                self.ui.lineEdit_A.setText("12")
                self.ui.lineEdit_Z.setText("1")
        TSolve.Solve(self)

    def chooseColormap(self, cmap):
        if cmap == "partical":
            self.cmap = CustomColormaps.particals
        elif cmap == "field":
            self.cmap = CustomColormaps.field
        else:
            self.cmap = cmap
        TDraw.DrawImage(self)

    def setX0(self):
        self.X0 = int(self.ui.lineEdit_X0.text())
        TSolve.Solve(self)

    def setY0(self):
        self.Y0 = int(self.ui.lineEdit_Y0.text())
        TSolve.Solve(self)

    def setdY(self):
        self.dY = int(self.ui.lineEdit_dY.text())
        TSolve.Solve(self)

    def setEmin(self):
        self.Emin = float(self.ui.lineEdit_Emin.text())
        TSolve.Solve(self)

    def setEmax(self):
        self.Emax = float(self.ui.lineEdit_Emax.text())
        TSolve.Solve(self)

    def setColormin(self):
        self.colormin = int(self.ui.lineEdit_colormin.text())
        TDraw.DrawImage(self)

    def setColormax(self):
        self.colormax = int(self.ui.lineEdit_colormax.text())
        TDraw.DrawImage(self)

    def setSpecEmin(self):
        self.specEmin = float(self.ui.lineEdit_specEmin.text())
        if self.isAnalysing == False:
            TDraw.DrawSpec(self)
        else:
            TDraw.DrawAnalysingSpec(self)

    def setSpecEmax(self):
        self.specEmax = float(self.ui.lineEdit_specEmax.text())
        if self.isAnalysing == False:
            TDraw.DrawSpec(self)
        else:
            TDraw.DrawAnalysingSpec(self)

    def setSpecdNdEmin(self):
        self.specdNdEmin = float(self.ui.lineEdit_specdNdEmin.text())
        if self.isAnalysing == False:
            TDraw.DrawSpec(self)
        else:
            TDraw.DrawAnalysingSpec(self)

    def setSpecdNdEmax(self):
        self.specdNdEmax = float(self.ui.lineEdit_specdNdEmax.text())
        if self.isAnalysing == False:
            TDraw.DrawSpec(self)
        else:
            TDraw.DrawAnalysingSpec(self)

    def setSpecWindow(self):
        self.specWindow = float(self.ui.lineEdit_specWindow.text())
        TSolve.Solve(self)

    def setSpectrumScale(self, index):
        self.spectrumScale = "linear" if index == 1 else "log"
        if self.isAnalysing == False:
            TDraw.DrawSpec(self)
        else:
            TDraw.DrawAnalysingSpec(self)

    def selectImage(self, item):
        if self.isAnalysing == False:
            self.isManualCursor = False
            self.specCursor = 0.0          # 换图后光标清零，截止能量需重新手动点选
            self.imageCursor = None
            self.imageFileName = os.path.join(self.imagePath, item.text())
            if self.useDefaultSavePath:
                self.ensureSavePath()
            TSolve.Solve(self)
            self.applyRememberedCutoff()
        else:
            list = self.ui.listWidget.selectedItems()
            self.analysingFileList = []
            for item in list:
                self.analysingFileList.append(os.path.join(self.imagePath, item.text()))
            TSolve.Solve(self)

    def cutoffMemoryKey(self, path=None):
        if path is None:
            path = self.imageFileName
        return os.path.basename(os.path.normpath(os.fspath(path)))

    def loadCutoffMemory(self):
        try:
            with open(self.cutoffMemoryFile, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}
        memory = {}
        for key, value in data.items():
            try:
                memory[str(key)] = float(value)
            except (TypeError, ValueError):
                pass
        return memory

    def saveCutoffMemory(self):
        try:
            with open(self.cutoffMemoryFile, "w", encoding="utf-8") as f:
                json.dump(self.cutoffMemory, f, ensure_ascii=False, indent=2, sort_keys=True)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "保存失败", f"截止能量记忆保存失败：{exc}")

    def applyRememberedCutoff(self):
        if self.imageFileName == "." or not os.path.isfile(self.imageFileName):
            return False
        key = self.cutoffMemoryKey()
        if key not in self.cutoffMemory:
            return False

        cutoff = float(self.cutoffMemory[key])
        self.isManualCursor = True
        self.cutoffEnergy = cutoff
        self.specCursor = cutoff
        self.imageCursor = TPSParabola.CutoffLine(self.TPS, self.specCursor, self.X0, self.A, self.Z)
        if self.zoomed:
            self.zoomImageCursor = TPSParabola.zoomCutoffLine(self.imageCursor, self.zoomToOrigin)
        self.ui.lineEdit_cutoff.setText(str(np.round(cutoff, 2)))
        TDraw.DrawImage(self)
        TDraw.DrawOrigin(self)
        TDraw.DrawSpec(self)
        self.drawActiveOverlays()
        print(f"[PyTPS] 已载入记忆截止能量：{key} -> {cutoff:.2f} MeV")
        return True

    def rememberCurrentCutoff(self):
        if self.isAnalysing:
            QtWidgets.QMessageBox.information(self, "提示", "批量分析模式下不保存单张图片截止能量。")
            return
        if self.imageFileName == "." or not os.path.isfile(self.imageFileName):
            QtWidgets.QMessageBox.information(self, "提示", "请先选择一张图片。")
            return
        if isinstance(getattr(self, "specCursor", None), str) or \
                not getattr(self, "specCursor", None):
            QtWidgets.QMessageBox.information(
                self, "提示", "请先在能谱图上点击选择截止能量（确认的是光标位置）。")
            return

        cutoff = float(np.round(self.specCursor, 2))
        key = self.cutoffMemoryKey()
        self.cutoffMemory[key] = cutoff
        self.cutoffEnergy = cutoff
        self.ui.lineEdit_cutoff.setText(str(cutoff))
        self.saveCutoffMemory()
        print(f"[PyTPS] 已记忆截止能量：{key} -> {cutoff:.2f} MeV")
        # 同步上报打靶日志系统（后台线程，不阻塞界面）
        threading.Thread(target=self.reportCutoffToLogSystem,
                         args=(key, cutoff), daemon=True).start()

    def showReportStatus(self, msg):
        """状态栏显示上报结果（信号槽保证线程安全）"""
        self.statusBar().showMessage(msg, 8000)

    def reportCutoffToLogSystem(self, filename, energy):
        """把确认的截止能量上报到打靶日志系统。
        1) 优先 POST 8767 填报页 /api/energy_remote：按文件名精确匹配发次行，
           8767 页面与 A 机表格同时更新（与人工在 8767 点"发送"完全等价）；
        2) helper 不可达/未匹配时，兜底直连 A 机 /api/energy，
           shot_time 用图片文件 mtime（与 helper 的发次时间算法一致）。"""
        energy_s = str(energy)
        try:
            energy_f = float(energy)          # 仅供提示信息格式化，避免 str 误用 :.2f
        except (TypeError, ValueError):
            energy_f = 0.0
        # ---- 1) 8767 helper ----
        try:
            body = json.dumps({"filename": filename,
                               "energy": energy_s,
                               "field": LOG_ENERGY_FIELD}).encode("utf-8")
            req = urllib.request.Request(
                LOG_HELPER_URL + "/api/energy_remote", data=body,
                headers={"Content-Type": "application/json"})
            j = json.loads(urllib.request.urlopen(req, timeout=5)
                           .read().decode("utf-8"))
            if j.get("ok"):
                msg = f"✔ 能量 {energy_f:.2f} MeV 已上报日志系统（{filename}）"
                print(f"[PyTPS] {msg}")
                self._reportSignal.done.emit(msg)
                return
            reason = j.get("message") or j.get("error") or "未知原因"
            print(f"[PyTPS] helper 未匹配：{reason}，尝试直连 A 机…")
        except Exception as exc:
            print(f"[PyTPS] helper 不可达（{exc}），尝试直连 A 机…")
        # ---- 2) 兜底直连 A 机 ----
        try:
            fp = self.imageFileName if os.path.basename(
                self.imageFileName) == filename else os.path.join(
                self.imagePath, filename)
            st = datetime.datetime.fromtimestamp(
                os.path.getmtime(fp)
            ).strftime("%Y-%m-%d %H:%M:%S")
            body = json.dumps({
                "shot_time": st, "energy": energy_s,
                "field": LOG_ENERGY_FIELD, "machine": "PyTPS",
                "window_sec": LOG_MATCH_WINDOW}).encode("utf-8")
            req = urllib.request.Request(
                LOG_SERVER_URL + "/api/energy", data=body,
                headers={"Content-Type": "application/json"})
            j = json.loads(urllib.request.urlopen(req, timeout=5)
                           .read().decode("utf-8"))
            if j.get("ok"):
                msg = f"✔ 能量 {energy_f:.2f} MeV 已直报 A 机（{filename}，按 mtime {st}）"
                print(f"[PyTPS] {msg}")
            else:
                msg = f"✘ A 机未匹配发次：{j.get('error')}（{filename}）"
                print(f"[PyTPS] {msg}")
        except Exception as exc:
            msg = f"✘ 能量上报失败：{exc}（{filename} = {energy_s} MeV）"
            print(f"[PyTPS] {msg}")
        self._reportSignal.done.emit(msg)

    def modeSwitch(self):
        button = self.sender()
        if self.ui.radioButton_online.isChecked() and button.text() == "在线模式":
            self.mode = "online"
            self.ensureSavePath()
            observer.schedule(event_handler, self.imagePath, recursive=False)
            self.ui.listWidget.setEnabled(False)
            timeformat = "%Y%m%d-%H%M%S"
            time = datetime.datetime.now().strftime(timeformat)
            self.onlineCutoffEnergyListName = os.path.normcase(
                os.path.join(self.savePath, "在线模式截止能量汇总_" + time + ".txt"))
            with open(self.onlineCutoffEnergyListName, 'w') as f:
                f.write("开始于" + time + "的在线模式，读取到的图片截止能量如下" + '\n')
        elif self.ui.radioButton_offline.isChecked() and button.text() == "离线模式":
            self.mode = "offline"
            observer.unschedule_all()
            self.ui.listWidget.setEnabled(True)
            timeformat = "%Y%m%d-%H%M%S"
            time = datetime.datetime.now().strftime(timeformat)
            if hasattr(self, "onlineCutoffEnergyListName"):
                self.ensureSavePath()
                with open(self.onlineCutoffEnergyListName, 'a') as f:
                    f.write("结束于" + time + '\n')

    def analyseMode(self):
        if self.ui.checkBox_analyse.isChecked():
            self.ui.radioButton_offline.setChecked(True)
            self.ui.radioButton_offline.setEnabled(False)
            self.ui.radioButton_online.setEnabled(False)
            self.ui.checkBox_autoSave.setChecked(False)
            self.ui.checkBox_autoSave.setEnabled(False)
            self.ui.pushButton_saveImage.setEnabled(False)
            self.ui.groupBox_4.setEnabled(False)
            self.ui.groupBox_5.setEnabled(False)
            self.isAnalysing = True
            self.ui.listWidget.setSelectionMode(QtWidgets.QListWidget.MultiSelection)
        else:
            self.ui.radioButton_offline.setEnabled(True)
            self.ui.radioButton_online.setEnabled(True)
            self.ui.checkBox_autoSave.setEnabled(True)
            self.ui.pushButton_saveImage.setEnabled(True)
            self.ui.groupBox_4.setEnabled(True)
            self.ui.groupBox_5.setEnabled(True)
            self.isAnalysing = False
            self.ui.listWidget.setSelectionMode(QtWidgets.QListWidget.SingleSelection)

    def showParabola(self):
        if self.ui.checkBox_parabola.isChecked():
            self.isShowParabola = True
            TDraw.DrawImage(self)
            TDraw.DrawSpec(self)
            TDraw.DrawParabola(self)
        else:
            self.isShowParabola = False
            TDraw.DrawImage(self)
            TDraw.DrawSpec(self)
        if self.zoomed == False:
            TDraw.DrawOrigin(self)

    def createParticleLabelCheckbox(self):
        self.checkBox_particleLabel = QtWidgets.QCheckBox(self.ui.groupBox_5)
        self.checkBox_particleLabel.setText("显示多离子种类")
        self.checkBox_particleLabel.setObjectName("checkBox_particleLabel")
        self.ui.verticalLayout_4.addWidget(self.checkBox_particleLabel)
        self.checkBox_particleLabel.stateChanged.connect(self.showParticleLabel)

        self.groupBox_particleLabels = QtWidgets.QGroupBox(self.ui.groupBox_5)
        self.groupBox_particleLabels.setTitle("离子标签（同荷质比合并）")
        self.groupBox_particleLabels.setObjectName("groupBox_particleLabels")
        self.gridLayout_particleLabels = QtWidgets.QGridLayout(self.groupBox_particleLabels)
        self.particleLabelChecks = {}
        labels = ["H-1", "C-6", "O-8", "O-7", "C-5", "O-6", "C-4", "O-5", "C-3", "O-4", "C-2", "C-1"]
        self.enabledParticleLabels = set(labels)
        for i, label in enumerate(labels):
            check = QtWidgets.QCheckBox(self.groupBox_particleLabels)
            check.setText(label)
            check.setChecked(True)
            check.stateChanged.connect(self.updateParticleLabelSelection)
            self.particleLabelChecks[label] = check
            self.gridLayout_particleLabels.addWidget(check, i // 3, i % 3)
        self.groupBox_particleLabels.setEnabled(False)
        self.ui.verticalLayout_4.addWidget(self.groupBox_particleLabels)

    def showParticleLabel(self):
        self.isShowParticleLabel = self.checkBox_particleLabel.isChecked()
        self.groupBox_particleLabels.setEnabled(self.isShowParticleLabel)
        TDraw.DrawImage(self)
        TDraw.DrawSpec(self)
        self.drawActiveOverlays()
        if self.zoomed == False:
            TDraw.DrawOrigin(self)

    def updateParticleLabelSelection(self):
        self.enabledParticleLabels = {
            label for label, check in self.particleLabelChecks.items()
            if check.isChecked()
        }
        if self.isShowParticleLabel:
            TDraw.DrawImage(self)
            TDraw.DrawSpec(self)
            self.drawActiveOverlays()
            if self.zoomed == False:
                TDraw.DrawOrigin(self)

    def drawActiveOverlays(self):
        if self.isShowParabola:
            TDraw.DrawParabola(self)
        elif self.isShowParticleLabel:
            TDraw.DrawSelectedParticleLabels(self)

    def autoSave(self):
        if self.ui.checkBox_autoSave.isChecked():
            self.isAutoSave = True
            self.saveImage()
            self.saveSpec()
        else:
            self.isAutoSave = False

    def saveSpec(self):
        if os.path.isfile(self.imageFileName):
            self.ensureSavePath()
            if self.isAnalysing == False:
                if isinstance(self.cutoffEnergy, str):
                    specPicFileName = os.path.normcase(os.path.join(self.savePath, os.path.splitext(
                        os.path.split(self.imageFileName)[1])[0] + "_" + "NaN.png"))
                    specDataFileName = os.path.normcase(os.path.join(self.savePath, os.path.splitext(
                        os.path.split(self.imageFileName)[1])[0] + "_" + "NaN.txt"))
                else:
                    ene = np.round(self.specCursor, 2)
                    specPicFileName = os.path.normcase(os.path.join(self.savePath, os.path.splitext(
                        os.path.split(self.imageFileName)[1])[0] + "_" + str(ene) + "MeV.png"))
                    specDataFileName = os.path.normcase(os.path.join(self.savePath, os.path.splitext(
                        os.path.split(self.imageFileName)[1])[0] + "_" + str(ene) + "MeV.txt"))
                self.specFig.savefig(specPicFileName, dpi=300)
                if hasattr(self, "specDataRaw") and len(self.specDataRaw) != 0:
                    net_values = self.specDataNet[1] if hasattr(self, "specDataNet") else self.specDataRaw[1]
                    specOut = np.column_stack((
                        self.specDataRaw[0],
                        self.specDataRaw[1],
                        net_values,
                        self.specData[1],
                        self.noiseSpec[1]
                    ))
                    np.savetxt(
                        specDataFileName, specOut, delimiter='\t', newline='\n',
                        header='Energy_MeV\td2N_dE_dOmega_raw_center\td2N_dE_dOmega_background_subtracted\td2N_dE_dOmega_smoothed\td2N_dE_dOmega_background',
                        encoding='utf-8'
                    )
                else:
                    np.savetxt(specDataFileName, self.specData.transpose(), delimiter='\t', newline='\n', encoding='utf-8')
            else:
                timeformat = "%Y%m%d-%H%M%S"
                time = datetime.datetime.now().strftime(timeformat)
                specPicFileName = os.path.normcase(os.path.join(self.savePath, "Analysing_" + time + ".png"))
                self.specFig.savefig(specPicFileName, dpi=300)

    def saveImage(self):
        if os.path.isfile(self.imageFileName):
            self.ensureSavePath()
            imagePicFileName = os.path.normcase(
                os.path.join(self.savePath, os.path.splitext(os.path.split(self.imageFileName)[1])[0] + "_color.png"))
            self.imageFig.savefig(imagePicFileName)

    def initZoom(self, event):
        self.zoomStart = [event.xdata, event.ydata]

    def zoom(self, event):
        if self.zoomStart[0] is None or self.zoomStart[1] is None or event.xdata is None or event.ydata is None:
            self.ui.pushButton_zoom.setChecked(False)
            return
        self.zoomEnd = [event.xdata, event.ydata]
        self.ui.pushButton_zoom.setChecked(False)

        if self.zoomed == False:
            self.zoomToOrigin = [0, 0, self.imageData[1].shape[0] - 1, self.imageData[0].shape[0] - 1]

        self.zoomed = True

        self.zoomArea = np.zeros(4)
        self.zoomArea[1] = np.min([np.ceil(self.zoomStart[0]), np.ceil(self.zoomEnd[0])])
        self.zoomArea[0] = np.min([np.ceil(self.zoomStart[1]), np.ceil(self.zoomEnd[1])])
        self.zoomArea[3] = np.max([np.ceil(self.zoomStart[0]), np.ceil(self.zoomEnd[0])])
        self.zoomArea[2] = np.max([np.ceil(self.zoomStart[1]), np.ceil(self.zoomEnd[1])])
        self.zoomArea = self.zoomArea.astype(int)

        new_area = [self.zoomArea[0] + self.zoomToOrigin[0], self.zoomArea[1] + self.zoomToOrigin[1],
                    self.zoomToOrigin[2] - (self.zoomPic.shape[0] - 1 - self.zoomArea[2]),
                    self.zoomToOrigin[3] - (self.zoomPic.shape[1] - 1 - self.zoomArea[3])]
        self.setImageZoomWindow(new_area)

    def setImageZoomWindow(self, area):
        height, width = self.imageData.shape
        y0, x0, y1, x1 = [int(v) for v in area]
        y0 = max(0, min(y0, height - 2))
        x0 = max(0, min(x0, width - 2))
        y1 = max(y0 + 1, min(y1, height - 1))
        x1 = max(x0 + 1, min(x1, width - 1))

        if y0 <= 0 and x0 <= 0 and y1 >= height - 1 and x1 >= width - 1:
            self.stopZoom()
            return

        self.zoomed = True
        self.zoomToOrigin = [y0, x0, y1, x1]
        self.zoomPic = self.imageData[y0:y1 + 1, x0:x1 + 1]
        self.zoomPara = TPSParabola.ZoomPara(self.TPSParabola, self.zoomToOrigin, self.zoomPic.shape[1])
        if hasattr(self, 'backgroundParabola'):
            self.zoomBackgroundPara = TPSParabola.ZoomPara(self.backgroundParabola, self.zoomToOrigin, self.zoomPic.shape[1])
        self.zoomImageCursor = TPSParabola.zoomCutoffLine(self.imageCursor, self.zoomToOrigin)

        TDraw.DrawImage(self)
        TDraw.DrawSpec(self)
        self.drawActiveOverlays()

    def wheelZoom(self, event):
        if event.inaxes != getattr(self, 'ax_image', None) or event.xdata is None or event.ydata is None:
            return
        if self.imageData is None or len(self.imageData) == 0:
            return

        height, width = self.imageData.shape
        if self.zoomed:
            cur_y0, cur_x0, cur_y1, cur_x1 = self.zoomToOrigin
            center_x = cur_x0 + event.xdata
            center_y = cur_y0 + event.ydata
            cur_w = cur_x1 - cur_x0 + 1
            cur_h = cur_y1 - cur_y0 + 1
        else:
            center_x = event.xdata
            center_y = event.ydata
            cur_w = width
            cur_h = height

        scale = 0.8 if event.button == 'up' else 1.25
        new_w = int(max(20, min(width, cur_w * scale)))
        new_h = int(max(20, min(height, cur_h * scale)))

        if new_w >= width - 1 and new_h >= height - 1:
            self.stopZoom()
            return

        x0 = int(round(center_x - new_w / 2))
        y0 = int(round(center_y - new_h / 2))
        x1 = x0 + new_w - 1
        y1 = y0 + new_h - 1

        if x0 < 0:
            x1 -= x0
            x0 = 0
        if y0 < 0:
            y1 -= y0
            y0 = 0
        if x1 >= width:
            x0 -= x1 - width + 1
            x1 = width - 1
        if y1 >= height:
            y0 -= y1 - height + 1
            y1 = height - 1

        self.setImageZoomWindow([y0, x0, y1, x1])

    def closeEvent(self, event):
        self.ui.radioButton_offline.setChecked(True)
        isSaveInit = QtWidgets.QMessageBox.question(self, "退出程序", "是否覆盖保存当前设置？",
                                                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                                    QtWidgets.QMessageBox.Yes)
        if isSaveInit == QtWidgets.QMessageBox.Yes:
            TSave.save_ini(self)
        self.close()

    def startZoom(self):
        if self.ui.pushButton_zoom.isChecked():
            self.pressId = self.imageFig.canvas.mpl_connect('button_press_event', self.initZoom)
            self.releaseId = self.imageFig.canvas.mpl_connect('button_release_event', self.zoom)
        else:
            self.imageFig.canvas.mpl_disconnect(self.pressId)
            self.imageFig.canvas.mpl_disconnect(self.releaseId)

    def stopZoom(self):
        self.zoomed = False
        self.zoomPic = self.imageData
        TDraw.DrawImage(self)
        TDraw.DrawOrigin(self)
        self.drawActiveOverlays()

    def setSpecCursor(self, event):
        if event.xdata is None:
            return
        self.isManualCursor = True
        if self.isAnalysing == False:
            self.specCursor = event.xdata
            # 截止能量框实时跟随光标（手动点击即所见即所得）
            self.ui.lineEdit_cutoff.setText(str(np.round(event.xdata, 2)))
            self.imageCursor = TPSParabola.CutoffLine(self.TPS, self.specCursor, self.X0, self.A, self.Z)
            if self.zoomed == True:
                self.zoomImageCursor = TPSParabola.zoomCutoffLine(self.imageCursor, self.zoomToOrigin)
            TDraw.DrawImage(self)
            TDraw.DrawOrigin(self)
            TDraw.DrawSpec(self)
            self.drawActiveOverlays()
        else:
            self.specCursor = event.xdata
            TDraw.DrawAnalysingSpec(self)

    def startManualMask(self):
        if self.imageFileName == "." or not os.path.isfile(self.imageFileName):
            return
        if self.ui.pushButton_zoom.isChecked():
            self.ui.pushButton_zoom.setChecked(False)
        self.isDrawingMask = True
        self.showManualMaskOverlay = True
        self.manualMaskPoints = []
        if self.maskClickId is None:
            self.maskClickId = self.imageFig.canvas.mpl_connect('button_press_event', self.addManualMaskPoint)
        print("[PyTPS] 开始手动画Mask：在图像上左键依次点多边形边界。")

    def addManualMaskPoint(self, event):
        if not self.isDrawingMask or event.inaxes != getattr(self, 'ax_image', None):
            return
        if event.xdata is None or event.ydata is None:
            return
        image_x, image_y = self.mapDisplayedPointToImage(event.xdata, event.ydata)
        self.manualMaskPoints.append((image_x, image_y))
        self.ax_image.plot(event.xdata, event.ydata, marker='o', color='lime', markersize=3)
        if len(self.manualMaskPoints) > 1:
            x0, y0 = self.mapImagePointToDisplayed(*self.manualMaskPoints[-2])
            self.ax_image.plot([x0, event.xdata], [y0, event.ydata], color='lime', linewidth=1)
        self.imageCanvas.draw_idle()

    def mapDisplayedPointToImage(self, x, y):
        if self.zoomed:
            return x + self.zoomToOrigin[1], y + self.zoomToOrigin[0]
        return x, y

    def mapImagePointToDisplayed(self, x, y):
        if self.zoomed:
            return x - self.zoomToOrigin[1], y - self.zoomToOrigin[0]
        return x, y

    def finishManualMaskPolygon(self):
        if len(self.manualMaskPoints) < 3:
            return
        self.manualMaskPolygons.append(list(self.manualMaskPoints))
        x0, y0 = self.mapImagePointToDisplayed(*self.manualMaskPoints[-1])
        x1, y1 = self.mapImagePointToDisplayed(*self.manualMaskPoints[0])
        self.ax_image.plot([x0, x1], [y0, y1], color='lime', linewidth=1)
        self.manualMaskPoints = []
        self.imageCanvas.draw_idle()
        print(f"[PyTPS] 已闭合Mask多边形，总数={len(self.manualMaskPolygons)}")

    def buildManualMask(self):
        return self.buildMaskFromPolygons(self.manualMaskPolygons)

    def buildMaskFromPolygons(self, polygons):
        if not polygons:
            return None
        from matplotlib.path import Path
        height, width = self.imageData.shape
        yy, xx = np.mgrid[:height, :width]
        points = np.column_stack((xx.ravel(), yy.ravel()))
        mask = np.zeros((height, width), dtype=bool)
        for polygon in polygons:
            path = Path(polygon)
            mask |= path.contains_points(points).reshape((height, width))
        return mask

    def applyManualMask(self):
        if self.isDrawingMask and len(self.manualMaskPoints) >= 3:
            self.finishManualMaskPolygon()
        mask = self.buildManualMask()
        if mask is None:
            return
        import TFilter
        self.manualMask = mask
        self.showManualMaskOverlay = False
        source = getattr(self, 'baseDisplayImage', self.imageData)
        self.displayImage = TFilter.applyManualMaskDisplay(
            source,
            mask,
            bg_sigma=getattr(self, 'maskBgSigma', 3),
            bg_percentile=getattr(self, 'maskBgPercentile', 35)
        )
        self.imageData = self.displayImage
        if self.zoomed:
            self.zoomPic = self.displayImage[
                self.zoomToOrigin[0]:self.zoomToOrigin[2] + 1,
                self.zoomToOrigin[1]:self.zoomToOrigin[3] + 1
            ]
        else:
            self.zoomPic = self.displayImage
        self.isDrawingMask = False
        TDraw.DrawImage(self)
        self.drawActiveOverlays()
        TDraw.DrawOrigin(self)
        print("[PyTPS] 已应用手工Mask：Mask内保留，Mask外匀化为背景。")

    def drawManualMaskOverlay(self):
        if not hasattr(self, 'ax_image'):
            return
        if self.manualMask is not None and getattr(self, 'showManualMaskOverlay', False):
            mask = self.manualMask
            if self.zoomed:
                mask = mask[
                    self.zoomToOrigin[0]:self.zoomToOrigin[2] + 1,
                    self.zoomToOrigin[1]:self.zoomToOrigin[3] + 1
                ]
            overlay = np.zeros((*mask.shape, 4), dtype=float)
            overlay[..., 1] = 1.0
            overlay[..., 3] = mask.astype(float) * 0.25
            self.ax_image.imshow(overlay, extent=(-0.5, mask.shape[1] - 0.5, mask.shape[0] - 0.5, -0.5))
        if getattr(self, 'showManualMaskOverlay', False) or self.isDrawingMask:
            self.drawManualMaskPolygons()

    def drawManualMaskPolygons(self):
        if not hasattr(self, 'ax_image'):
            return
        for polygon in self.manualMaskPolygons:
            if len(polygon) < 2:
                continue
            pts = [self.mapImagePointToDisplayed(x, y) for x, y in polygon]
            xs = [p[0] for p in pts] + [pts[0][0]]
            ys = [p[1] for p in pts] + [pts[0][1]]
            self.ax_image.plot(xs, ys, color='lime', linewidth=1)
            self.ax_image.plot(xs[:-1], ys[:-1], linestyle='', marker='o', color='lime', markersize=3)
        if len(self.manualMaskPoints) > 0:
            pts = [self.mapImagePointToDisplayed(x, y) for x, y in self.manualMaskPoints]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            self.ax_image.plot(xs, ys, color='lime', linewidth=1)
            self.ax_image.plot(xs, ys, linestyle='', marker='o', color='lime', markersize=3)

    def clearManualMask(self):
        self.isDrawingMask = False
        self.manualMaskPoints = []
        self.manualMaskPolygons = []
        self.manualMask = None
        self.showManualMaskOverlay = False
        if hasattr(self, 'baseDisplayImage'):
            self.displayImage = self.baseDisplayImage.copy()
            self.imageData = self.displayImage
            if self.zoomed:
                self.zoomPic = self.displayImage[
                    self.zoomToOrigin[0]:self.zoomToOrigin[2] + 1,
                    self.zoomToOrigin[1]:self.zoomToOrigin[3] + 1
                ]
            else:
                self.zoomPic = self.displayImage
            TDraw.DrawImage(self)
            self.drawActiveOverlays()
            TDraw.DrawOrigin(self)
        print("[PyTPS] 已清除手工Mask。")

    def setMaskBgSigma(self):
        try:
            value = float(self.lineEdit_maskBgSigma.text())
            value = max(0.0, min(50.0, value))
            self.maskBgSigma = value
            self.lineEdit_maskBgSigma.setText(str(value))
        except ValueError:
            self.lineEdit_maskBgSigma.setText(str(self.maskBgSigma))

    def setMaskBgPercentile(self):
        try:
            value = float(self.lineEdit_maskBgPercentile.text())
            value = max(0.0, min(100.0, value))
            self.maskBgPercentile = value
            self.lineEdit_maskBgPercentile.setText(str(value))
        except ValueError:
            self.lineEdit_maskBgPercentile.setText(str(self.maskBgPercentile))

    def setBackgroundMode(self, index):
        self.backgroundMode = "offset" if index == 0 else "sideband"
        if self.imageFileName != "." and os.path.isfile(self.imageFileName):
            TSolve.Solve(self)

    def setBackgroundOffset(self):
        try:
            value = float(self.lineEdit_bgOffset.text())
            value = max(-200.0, min(200.0, value))
            self.backgroundOffset = value
            self.lineEdit_bgOffset.setText(str(value))
            if self.imageFileName != "." and os.path.isfile(self.imageFileName):
                TSolve.Solve(self)
        except ValueError:
            self.lineEdit_bgOffset.setText(str(self.backgroundOffset))

    def setBackgroundWidth(self):
        try:
            value = float(self.lineEdit_bgWidth.text())
            value = max(1.0, min(100.0, value))
            self.backgroundWidth = value
            self.lineEdit_bgWidth.setText(str(value))
            if self.imageFileName != "." and os.path.isfile(self.imageFileName):
                TSolve.Solve(self)
        except ValueError:
            self.lineEdit_bgWidth.setText(str(self.backgroundWidth))

    def setSpectrumSmoothWindow(self):
        try:
            value = int(self.lineEdit_smoothWindow.text())
            value = max(3, min(201, value))
            if value % 2 == 0:
                value += 1
            self.spectrumSmoothWindow = value
            self.lineEdit_smoothWindow.setText(str(value))
            if self.imageFileName != "." and os.path.isfile(self.imageFileName):
                TSolve.Solve(self)
        except ValueError:
            self.lineEdit_smoothWindow.setText(str(self.spectrumSmoothWindow))

    def saveManualMask(self):
        if self.manualMask is None:
            mask = self.buildManualMask()
            if mask is None:
                return
            self.manualMask = mask

        mask_dir = os.path.join(self.savePath, "masks")
        os.makedirs(mask_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.imageFileName))[0]
        mask_path = os.path.join(mask_dir, stem + "_mask.png")
        overlay_path = os.path.join(mask_dir, stem + "_overlay.png")
        json_path = os.path.join(mask_dir, stem + "_mask.json")

        from imageio.v3 import imwrite
        import json

        saved_polygons = []
        if os.path.isfile(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                old_metadata = json.load(f)
            saved_polygons = old_metadata.get("polygons", [])

        new_polygons = [
            [[float(x), float(y)] for x, y in polygon]
            for polygon in self.manualMaskPolygons
        ]
        merged_polygons = []
        seen = set()
        for polygon in saved_polygons + new_polygons:
            key = tuple((round(float(x), 3), round(float(y), 3)) for x, y in polygon)
            if key in seen:
                continue
            seen.add(key)
            merged_polygons.append(polygon)
        merged_mask = self.buildMaskFromPolygons(merged_polygons)
        if merged_mask is not None:
            self.manualMask = merged_mask
            self.manualMaskPolygons = merged_polygons

        mask_img = (self.manualMask.astype(np.uint8) * 255)
        imwrite(mask_path, mask_img)

        fig = plt.figure()
        ax = fig.subplots()
        ax.imshow(getattr(self, 'rawImage', self.imageData), cmap=self.cmap, vmin=self.colormin, vmax=self.colormax)
        overlay = np.zeros((*self.manualMask.shape, 4), dtype=float)
        overlay[..., 1] = 1.0
        overlay[..., 3] = self.manualMask.astype(float) * 0.35
        ax.imshow(overlay)
        ax.axis('off')
        fig.savefig(overlay_path, bbox_inches='tight', pad_inches=0)
        plt.close(fig)

        metadata = {
            "image": os.path.basename(self.imageFileName),
            "image_path": self.imageFileName,
            "mask": os.path.basename(mask_path),
            "overlay": os.path.basename(overlay_path),
            "shape": list(self.manualMask.shape),
            "label": "trajectory",
            "polygons": merged_polygons
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        print(f"[PyTPS] 已保存Mask: {mask_path}")

    def loadManualMask(self):
        start_dir = os.path.join(self.savePath, "masks") if self.savePath else "."
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "读取Mask",
            start_dir,
            "Mask files (*.png *.tif *.tiff *.jpg *.json);;All files (*.*)"
        )
        if not path:
            return

        mask_path = path
        loaded_polygons = []
        if path.lower().endswith(".json"):
            import json
            with open(path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            mask_name = metadata.get("mask")
            loaded_polygons = metadata.get("polygons", [])
            if not mask_name:
                QtWidgets.QMessageBox.warning(self, "读取Mask", "JSON中没有mask字段。")
                return
            mask_path = os.path.join(os.path.dirname(path), mask_name)

        from imageio.v3 import imread
        mask_img = imread(mask_path)
        if mask_img.ndim == 3:
            mask_img = mask_img[..., 0]
        mask = mask_img > 0

        if self.imageData is None or len(self.imageData) == 0:
            QtWidgets.QMessageBox.warning(self, "读取Mask", "请先加载一张图像。")
            return
        if mask.shape != self.imageData.shape:
            QtWidgets.QMessageBox.warning(
                self,
                "读取Mask",
                f"Mask尺寸{mask.shape}与当前图像尺寸{self.imageData.shape}不一致。"
            )
            return

        self.manualMask = mask
        self.showManualMaskOverlay = True
        self.manualMaskPoints = []
        self.manualMaskPolygons = loaded_polygons
        TDraw.DrawImage(self)
        self.drawActiveOverlays()
        TDraw.DrawOrigin(self)
        print(f"[PyTPS] 已读取Mask: {mask_path}")

    def help(self):
        title = "帮助"
        text = ("自动解谱功能的设置：\n“解谱设置-±dY”对应解谱时考虑的谱线范围，对于信号较弱的图片（尤其是8位图），可以考虑调大这个值。 \
        \n“自动解谱窗口”是指自动寻找截止能量时，有多大范围的能谱始终高于本底，就确定此处为截止能量。能谱线的噪声较大时，可以考虑调大这个值。")
        helpWindow = QtWidgets.QMessageBox.about(self, title, text)

    def about(self):
        title = "关于"
        text = ("PyTPS 汤姆逊谱仪全自动解谱程序 v2.1\n作者：张予嘉\n本程序改进自MATTPS V3.0\n特别感谢：MATTPS作者：徐诗睿师兄\n2022.11.8 \
        \n\nv2.1更新：\n保存：新增了保存能谱线原始数据的功能 \
        \n\nv2.0更新：\n自动寻找截止能量：全面重制的自动截止能量功能！直接基于对本底的对比寻找截止能量，更加稳定更加准确\n解谱：优化了对于8位图的去噪算法，使得弱信号更容易被分辨出来\n在线模式：修复了监视进程容易崩溃的bug \
        \n\nv1.1更新：\n自动解谱：优化了对于8位图的自动截止能量处理\n新功能：在线模式下可以自动触发Andor相机")
        aboutWindow = QtWidgets.QMessageBox.about(self, title, text)

    # 滤波功能相关方法（新增）

    def createFilterUI(self):
        """动态创建滤波设置UI（简化版：形态学阈值分割）"""
        # 创建滤波设置GroupBox
        self.groupBox_filter = QtWidgets.QGroupBox(self.ui.groupBox_2)
        self.groupBox_filter.setObjectName("groupBox_filter")
        self.groupBox_filter.setTitle("显示/Mask 设置")

        # 创建垂直布局
        self.verticalLayout_filter = QtWidgets.QVBoxLayout(self.groupBox_filter)

        # 滤波模式选择
        self.horizontalLayout_filterMode = QtWidgets.QHBoxLayout()
        self.label_filterMode = QtWidgets.QLabel(self.groupBox_filter)
        self.label_filterMode.setText("显示滤波：")
        self.comboBox_filterMode = QtWidgets.QComboBox(self.groupBox_filter)
        self.comboBox_filterMode.addItems(["原图", "中值3x3", "中值5x5"])
        # 根据配置设置初始值
        if self.filterMode in ("none", "raw"):
            self.comboBox_filterMode.setCurrentIndex(0)
            self.filterMode = "none"
        elif self.filterMode == "median5":
            self.comboBox_filterMode.setCurrentIndex(2)
        else:
            # 旧配置里的protected/形态学模式迁移为保守的3x3显示中值。
            self.comboBox_filterMode.setCurrentIndex(1)
            self.filterMode = "median3"
        self.horizontalLayout_filterMode.addWidget(self.label_filterMode)
        self.horizontalLayout_filterMode.addWidget(self.comboBox_filterMode)
        self.verticalLayout_filter.addLayout(self.horizontalLayout_filterMode)

        self.label_analysisPreprocess = QtWidgets.QLabel(self.groupBox_filter)
        self.label_analysisPreprocess.setText("定量解谱固定使用：3x3热像素中值；下方显示滤波/Mask不改变能谱。")
        self.label_analysisPreprocess.setStyleSheet("color: #555; font-size: 9pt;")
        self.label_analysisPreprocess.setWordWrap(True)
        self.verticalLayout_filter.addWidget(self.label_analysisPreprocess)

        # 轨迹坐标背景减除按钮（只用于显示清理）
        self.pushButton_trajectoryBgSubtract = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_trajectoryBgSubtract.setText("理论轨迹显示清理")
        self.pushButton_trajectoryBgSubtract.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 10px; }")
        self.verticalLayout_filter.addWidget(self.pushButton_trajectoryBgSubtract)

        self.groupBox_manualMask = QtWidgets.QGroupBox(self.groupBox_filter)
        self.groupBox_manualMask.setTitle("手工Mask")
        self.verticalLayout_manualMaskBox = QtWidgets.QVBoxLayout(self.groupBox_manualMask)

        self.horizontalLayout_manualMask = QtWidgets.QHBoxLayout()
        self.pushButton_startMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_startMask.setText("开始绘制")
        self.pushButton_finishMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_finishMask.setText("闭合")
        self.pushButton_applyMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_applyMask.setText("应用显示")
        self.pushButton_saveMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_saveMask.setText("保存")
        self.pushButton_loadMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_loadMask.setText("读取")
        self.pushButton_clearMask = QtWidgets.QPushButton(self.groupBox_filter)
        self.pushButton_clearMask.setText("清除")
        self.horizontalLayout_manualMask.addWidget(self.pushButton_startMask)
        self.horizontalLayout_manualMask.addWidget(self.pushButton_finishMask)
        self.horizontalLayout_manualMask.addWidget(self.pushButton_applyMask)
        self.horizontalLayout_manualMask2 = QtWidgets.QHBoxLayout()
        self.horizontalLayout_manualMask2.addWidget(self.pushButton_saveMask)
        self.horizontalLayout_manualMask2.addWidget(self.pushButton_loadMask)
        self.horizontalLayout_manualMask2.addWidget(self.pushButton_clearMask)
        self.verticalLayout_manualMaskBox.addLayout(self.horizontalLayout_manualMask)
        self.verticalLayout_manualMaskBox.addLayout(self.horizontalLayout_manualMask2)

        self.horizontalLayout_maskParams = QtWidgets.QHBoxLayout()
        self.label_maskBgSigma = QtWidgets.QLabel(self.groupBox_filter)
        self.label_maskBgSigma.setText("Mask背景半径：")
        self.lineEdit_maskBgSigma = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_maskBgSigma.setText(str(self.maskBgSigma))
        self.lineEdit_maskBgSigma.setValidator(QtGui.QDoubleValidator(0.0, 50.0, 2))
        self.lineEdit_maskBgSigma.setMaximumWidth(55)
        self.label_maskBgPercentile = QtWidgets.QLabel(self.groupBox_filter)
        self.label_maskBgPercentile.setText("背景分位：")
        self.lineEdit_maskBgPercentile = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_maskBgPercentile.setText(str(self.maskBgPercentile))
        self.lineEdit_maskBgPercentile.setValidator(QtGui.QDoubleValidator(0.0, 100.0, 2))
        self.lineEdit_maskBgPercentile.setMaximumWidth(55)
        self.horizontalLayout_maskParams.addWidget(self.label_maskBgSigma)
        self.horizontalLayout_maskParams.addWidget(self.lineEdit_maskBgSigma)
        self.horizontalLayout_maskParams.addWidget(self.label_maskBgPercentile)
        self.horizontalLayout_maskParams.addWidget(self.lineEdit_maskBgPercentile)
        self.verticalLayout_manualMaskBox.addLayout(self.horizontalLayout_maskParams)
        self.verticalLayout_filter.addWidget(self.groupBox_manualMask)

        self.horizontalLayout_bgMode = QtWidgets.QHBoxLayout()
        self.label_bgMode = QtWidgets.QLabel(self.groupBox_filter)
        self.label_bgMode.setText("能谱背景：")
        self.comboBox_bgMode = QtWidgets.QComboBox(self.groupBox_filter)
        self.comboBox_bgMode.addItems(["同形偏移", "上下旁带"])
        self.comboBox_bgMode.setCurrentIndex(0 if self.backgroundMode == "offset" else 1)
        self.horizontalLayout_bgMode.addWidget(self.label_bgMode)
        self.horizontalLayout_bgMode.addWidget(self.comboBox_bgMode)
        self.verticalLayout_filter.addLayout(self.horizontalLayout_bgMode)

        self.horizontalLayout_bgParams = QtWidgets.QHBoxLayout()
        self.label_bgOffset = QtWidgets.QLabel(self.groupBox_filter)
        self.label_bgOffset.setText("偏移(px)：")
        self.lineEdit_bgOffset = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_bgOffset.setText(str(self.backgroundOffset))
        self.lineEdit_bgOffset.setValidator(QtGui.QDoubleValidator(-200.0, 200.0, 2))
        self.lineEdit_bgOffset.setMaximumWidth(60)
        self.label_bgWidth = QtWidgets.QLabel(self.groupBox_filter)
        self.label_bgWidth.setText("宽度(px)：")
        self.lineEdit_bgWidth = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_bgWidth.setText(str(self.backgroundWidth))
        self.lineEdit_bgWidth.setValidator(QtGui.QDoubleValidator(1.0, 100.0, 2))
        self.lineEdit_bgWidth.setMaximumWidth(60)
        self.horizontalLayout_bgParams.addWidget(self.label_bgOffset)
        self.horizontalLayout_bgParams.addWidget(self.lineEdit_bgOffset)
        self.horizontalLayout_bgParams.addWidget(self.label_bgWidth)
        self.horizontalLayout_bgParams.addWidget(self.lineEdit_bgWidth)
        self.verticalLayout_filter.addLayout(self.horizontalLayout_bgParams)

        self.horizontalLayout_smooth = QtWidgets.QHBoxLayout()
        self.label_smoothWindow = QtWidgets.QLabel(self.groupBox_filter)
        self.label_smoothWindow.setText("能谱平滑窗口：")
        self.lineEdit_smoothWindow = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_smoothWindow.setText(str(self.spectrumSmoothWindow))
        self.lineEdit_smoothWindow.setValidator(QtGui.QIntValidator(3, 201))
        self.lineEdit_smoothWindow.setMaximumWidth(60)
        self.horizontalLayout_smooth.addWidget(self.label_smoothWindow)
        self.horizontalLayout_smooth.addWidget(self.lineEdit_smoothWindow)
        self.horizontalLayout_smooth.addStretch()
        self.verticalLayout_filter.addLayout(self.horizontalLayout_smooth)

        self.horizontalLayout_spectrumScale = QtWidgets.QHBoxLayout()
        self.label_spectrumScale = QtWidgets.QLabel(self.groupBox_filter)
        self.label_spectrumScale.setText("能谱坐标：")
        self.comboBox_spectrumScale = QtWidgets.QComboBox(self.groupBox_filter)
        self.comboBox_spectrumScale.addItems(["半对数", "线性"])
        self.comboBox_spectrumScale.setCurrentIndex(1 if self.spectrumScale == "linear" else 0)
        self.horizontalLayout_spectrumScale.addWidget(self.label_spectrumScale)
        self.horizontalLayout_spectrumScale.addWidget(self.comboBox_spectrumScale)
        self.horizontalLayout_spectrumScale.addStretch()
        self.verticalLayout_filter.addLayout(self.horizontalLayout_spectrumScale)

        # 轨迹保护宽度（新增）
        self.horizontalLayout_protectionWidth = QtWidgets.QHBoxLayout()
        self.label_protectionWidth = QtWidgets.QLabel(self.groupBox_filter)
        self.label_protectionWidth.setText("轨迹保护宽度：")
        self.lineEdit_protectionWidth = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_protectionWidth.setText(str(self.protectionWidth))
        self.lineEdit_protectionWidth.setValidator(QtGui.QIntValidator(0, 30))
        self.lineEdit_protectionWidth.setMaximumWidth(80)
        self.label_protectionWidthUnit = QtWidgets.QLabel(self.groupBox_filter)
        self.label_protectionWidthUnit.setText("像素（从轨迹中心线向上下各保护N像素）")
        self.label_protectionWidthUnit.setStyleSheet("color: #666; font-size: 9pt;")
        self.horizontalLayout_protectionWidth.addWidget(self.label_protectionWidth)
        self.horizontalLayout_protectionWidth.addWidget(self.lineEdit_protectionWidth)
        self.horizontalLayout_protectionWidth.addWidget(self.label_protectionWidthUnit)
        self.horizontalLayout_protectionWidth.addStretch()
        self.verticalLayout_filter.addLayout(self.horizontalLayout_protectionWidth)

        # 羽化半径（原"条带半宽"）
        self.horizontalLayout_fadeRadius = QtWidgets.QHBoxLayout()
        self.label_fadeRadius = QtWidgets.QLabel(self.groupBox_filter)
        self.label_fadeRadius.setText("羽化半径：")
        self.lineEdit_fadeRadius = QtWidgets.QLineEdit(self.groupBox_filter)
        self.lineEdit_fadeRadius.setText(str(getattr(self, 'fadeRadius', 20)))
        self.lineEdit_fadeRadius.setValidator(QtGui.QIntValidator(0, 50))
        self.lineEdit_fadeRadius.setMaximumWidth(80)
        self.label_fadeRadiusUnit = QtWidgets.QLabel(self.groupBox_filter)
        self.label_fadeRadiusUnit.setText("像素（从保护边缘往外柔和过渡距离）")
        self.label_fadeRadiusUnit.setStyleSheet("color: #666; font-size: 9pt;")
        self.horizontalLayout_fadeRadius.addWidget(self.label_fadeRadius)
        self.horizontalLayout_fadeRadius.addWidget(self.lineEdit_fadeRadius)
        self.horizontalLayout_fadeRadius.addWidget(self.label_fadeRadiusUnit)
        self.horizontalLayout_fadeRadius.addStretch()
        self.verticalLayout_filter.addLayout(self.horizontalLayout_fadeRadius)

        # 将滤波GroupBox添加到主布局
        self.ui.verticalLayout_3.addWidget(self.groupBox_filter)

        # 绑定信号
        self.comboBox_filterMode.currentIndexChanged.connect(self.setFilterMode)
        self.pushButton_trajectoryBgSubtract.clicked.connect(self.trajectoryBackgroundSubtraction)
        self.pushButton_startMask.clicked.connect(self.startManualMask)
        self.pushButton_finishMask.clicked.connect(self.finishManualMaskPolygon)
        self.pushButton_applyMask.clicked.connect(self.applyManualMask)
        self.pushButton_saveMask.clicked.connect(self.saveManualMask)
        self.pushButton_loadMask.clicked.connect(self.loadManualMask)
        self.pushButton_clearMask.clicked.connect(self.clearManualMask)
        self.lineEdit_maskBgSigma.editingFinished.connect(self.setMaskBgSigma)
        self.lineEdit_maskBgPercentile.editingFinished.connect(self.setMaskBgPercentile)
        self.comboBox_bgMode.currentIndexChanged.connect(self.setBackgroundMode)
        self.lineEdit_bgOffset.editingFinished.connect(self.setBackgroundOffset)
        self.lineEdit_bgWidth.editingFinished.connect(self.setBackgroundWidth)
        self.lineEdit_smoothWindow.editingFinished.connect(self.setSpectrumSmoothWindow)
        self.comboBox_spectrumScale.currentIndexChanged.connect(self.setSpectrumScale)
        self.lineEdit_protectionWidth.editingFinished.connect(self.setProtectionWidth)
        self.lineEdit_fadeRadius.editingFinished.connect(self.setFadeRadius)

        # 初始化UI状态
        self.updateFilterUIState()

    def updateFilterUIState(self):
        """根据当前滤波模式更新UI状态"""
        self.pushButton_trajectoryBgSubtract.setEnabled(True)
        self.lineEdit_protectionWidth.setEnabled(True)
        self.lineEdit_fadeRadius.setEnabled(True)

    def setProtectionWidth(self):
        """设置轨迹保护宽度"""
        try:
            width = int(self.lineEdit_protectionWidth.text())
            width = max(0, min(30, width))  # 允许设为0
            self.protectionWidth = width
            self.lineEdit_protectionWidth.setText(str(width))
            print(f"[PyTPS] 设置轨迹保护宽度：{width} 像素（从轨迹中心线向上下各保护{width}像素）")
        except ValueError:
            self.lineEdit_protectionWidth.setText(str(self.protectionWidth))

    def setFadeRadius(self):
        """设置羽化半径"""
        try:
            radius = int(self.lineEdit_fadeRadius.text())
            radius = max(0, min(50, radius))  # 允许设为0
            self.fadeRadius = radius
            self.lineEdit_fadeRadius.setText(str(radius))
            print(f"[PyTPS] 设置羽化半径：{radius} 像素")
        except ValueError:
            self.lineEdit_fadeRadius.setText(str(getattr(self, 'fadeRadius', 20)))

    def trajectoryBackgroundSubtraction(self):
        """理论轨迹显示清理：只更新预览图，不改变定量能谱。"""
        print("[PyTPS] 点击理论轨迹显示清理")

        # 直接读取当前图像
        if self.imageFileName == "." or not os.path.isfile(self.imageFileName):
            print("[PyTPS] 错误：没有加载图像")
            return

        import TImageRead, TFilter
        img, _ = TImageRead.readImage(self.imageFileName)
        img = img.astype(np.float64)

        # 调用新的多轨迹软掩膜滤波函数
        # 保护所有常见粒子（H-1, C-4/5/6, O-6/7/8）+ 原点
        print(f"[PyTPS] 开始理论轨迹显示清理：保护H/C/O所有轨迹+原点")

        fadeRadius = getattr(self, 'fadeRadius', 20)  # 羽化半径
        protectionWidth = self.protectionWidth  # 轨迹保护宽度

        img_cleaned = TFilter.softMaskMultiTrajectory(
            img,
            X0=self.X0,
            Y0=self.Y0,
            Emin=self.Emin,
            Emax=self.Emax,
            dY=self.dY,
            TPS=self.TPS,
            protection_width=protectionWidth,
            fade_radius=fadeRadius,
            rolling_ball_radius=40  # 保留参数兼容性，但不使用
        )

        # 只更新显示图。定量能谱继续使用TSolve生成的analysisImage。
        self.displayImage = np.flipud(img_cleaned)
        self.imageData = self.displayImage
        self.zoomPic = self.displayImage

        TDraw.DrawImage(self)
        TDraw.DrawSpec(self)
        self.drawActiveOverlays()
        TDraw.DrawOrigin(self)

        print(f"[PyTPS] 理论轨迹显示清理完成，仅更新显示图，未改变能谱")

    def setFilterMode(self, index):
        """设置显示滤波模式。定量解谱不受这里影响。"""
        if index == 0:
            self.filterMode = "none"
        elif index == 1:
            self.filterMode = "median3"
        elif index == 2:
            self.filterMode = "median5"
        self.updateFilterUIState()
        # 自动应用滤波
        print(f"显示滤波模式切换为: {self.filterMode}")
        if self.imageFileName != "." and os.path.isfile(self.imageFileName):
            TSolve.Solve(self)

    def createCutoffMemoryButton(self):
        """创建确认截止能量按钮"""
        self.ui.label_16.setText("截止能量(光标处)：")   # 原"仅供参考"框改为显示手动光标能量
        self.pushButton_rememberCutoff = QtWidgets.QPushButton(self.ui.groupBox_7)
        self.pushButton_rememberCutoff.setText("确认截止能量")
        self.pushButton_rememberCutoff.setObjectName("pushButton_rememberCutoff")
        self.pushButton_rememberCutoff.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; font-weight: bold; padding: 5px; }"
        )
        self.ui.verticalLayout_spec.addWidget(self.pushButton_rememberCutoff)
        self.pushButton_rememberCutoff.clicked.connect(self.rememberCurrentCutoff)

    def createCompareButton(self):
        """创建谱线对比按钮"""
        # 在帮助按钮旁边添加一个对比按钮
        # 找到帮助按钮所在的布局
        # 根据UI文件，帮助和关于按钮应该在某个水平布局中
        # 我们可以在verticalLayout_3中添加一个新的按钮

        self.pushButton_compare = QtWidgets.QPushButton(self.ui.groupBox_2)
        self.pushButton_compare.setText("质子谱线对比")
        self.pushButton_compare.setObjectName("pushButton_compare")
        self.pushButton_compare.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 5px; }")

        # 将按钮添加到verticalLayout_3的底部
        self.ui.verticalLayout_3.addWidget(self.pushButton_compare)

        # 绑定信号
        self.pushButton_compare.clicked.connect(self.openCompareWindow)

    def openCompareWindow(self):
        """打开谱线对比窗口"""
        if self.compareWindow is None:
            self.compareWindow = TCompare.CompareWindow(self)
            self.compareWindow.setParentApp(self)

        # 更新文件列表
        import os
        fileList = []
        if os.path.exists(self.imagePath):
            allFiles = os.listdir(self.imagePath)
            for f in allFiles:
                if f.endswith(".tif") or f.endswith(".tiff") or f.endswith(".jpg") or f.endswith(".png") or f.endswith(".raw"):
                    fileList.append(f)

        self.compareWindow.updateFileList(fileList)
        self.compareWindow.show()
        self.compareWindow.raise_()
        self.compareWindow.activateWindow()

        print("[PyTPS] 已打开谱线对比窗口")

    def applyFilterAndResolve(self):
        """应用滤波并重新解谱"""
        print(f"点击应用滤波按钮 - 当前模式: {self.filterMode}, medianSize: {self.medianSize}, radius: {self.rollingBallRadius}")
        print(f"当前图片文件: {self.imageFileName}")

        if self.imageFileName != "." and os.path.isfile(self.imageFileName):
            print("开始重新解谱...")
            TSolve.Solve(self)
            print("解谱完成")
        else:
            print("警告：未选择图片")
            QtWidgets.QMessageBox.warning(self, "警告", "请先选择一张图片！")


if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    mainwindow = MainWindow()

    event_handler = TWatchDog.MyHandler(mainwindow)
    observer = Observer()
    observer.start()

    mainwindow.show()
    sys.exit(app.exec())
